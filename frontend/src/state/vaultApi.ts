import { boundedJson } from "../clients/boundedResponse";
import { type DisplayMetadata, parseDisplayMetadata } from "./noteMetadata";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX = 13 * 1024 * 1024;
const MAX_WRITE = 2 * 1024 * 1024;
const TIMEOUT = 15_000;
export const SHA256 = /^[0-9a-f]{64}$/;
const PHYSICAL_ID = /^[0-9]+:[0-9]+$/;
const validRef = (value: unknown): value is string =>
  typeof value === "string" &&
  value.length > 0 &&
  // Code points, not UTF-16 units, to match the server's `len(ref)` (an astral character is one
  // Python `str` character but two UTF-16 units).
  [...value].length <= 512 &&
  !value.startsWith("/") &&
  !value.split("/").some((part) => part === "." || part === "..") &&
  !value.includes("\\") &&
  !value.includes(":") &&
  !Array.from(value).some((character) => (character.codePointAt(0) ?? 0) < 32);
// Mirrors the server's write-time predicate (src/viki_agent/vaults/vault.py excluded_part(write=True), plus the
// server's leaf-suffix and NTFS/device-name rules) so the UI refuses exactly what the server
// would. EXCLUDED_DIRS/PRIVATE_NAMES/WRITE_ONLY_NAMES/STAGE_PREFIX/BACKUP_PREFIX below name the
// same sets as src/viki_agent/vaults/vault.py, compared with toLowerCase(), which does not fold `ſ` the way Python's
// casefold() does; the server may still refuse a few exotic spellings this mirror would pass.
const EXCLUDED_DIRS = new Set([".claude", ".codex", ".git", ".viki"]);
const PRIVATE_NAMES = new Set([
  "credentials",
  "credentials.json",
  "." + "credentials.json",
  "secrets.json",
  ".env",
  ".env.local",
]);
const WRITE_ONLY_NAMES = new Set([
  "agents.md",
  "agents.override.md",
  "claude.md",
  "claude.local.md",
  "gemini.md",
]);
const STAGE_PREFIX = ".viki-stage-";
const BACKUP_PREFIX = ".viki-backup-";
// NTFS refuses these characters; a part over 255 UTF-16 units cannot round-trip either.
const NTFS_REFUSED = /["*<>?|]/;
const MAX_PART_UNITS = 255;
// Windows device names (os.path.isreserved's set: neither COM0 nor LPT0 is reserved):
// compared against the part's text before its first dot, trailing spaces dropped, with
// toUpperCase() as Python's `.upper()` does (both map a dotless "ı" to "I").
const DEVICE_NAMES = new Set([
  "CON",
  "PRN",
  "AUX",
  "NUL",
  "COM1",
  "COM2",
  "COM3",
  "COM4",
  "COM5",
  "COM6",
  "COM7",
  "COM8",
  "COM9",
  "COM¹",
  "COM²",
  "COM³",
  "LPT1",
  "LPT2",
  "LPT3",
  "LPT4",
  "LPT5",
  "LPT6",
  "LPT7",
  "LPT8",
  "LPT9",
  "LPT¹",
  "LPT²",
  "LPT³",
  "CONIN$",
  "CONOUT$",
]);
const deviceStem = (part: string): string => {
  const dot = part.indexOf(".");
  return (dot === -1 ? part : part.slice(0, dot)).replace(/ +$/, "").toUpperCase();
};
const excludedWritePart = (part: string): boolean => {
  const lowered = part.toLowerCase();
  return (
    part === "" ||
    NTFS_REFUSED.test(part) ||
    part.length > MAX_PART_UNITS ||
    part.endsWith(".") ||
    part.endsWith(" ") ||
    EXCLUDED_DIRS.has(lowered) ||
    PRIVATE_NAMES.has(lowered) ||
    lowered.startsWith(".env.") ||
    WRITE_ONLY_NAMES.has(lowered) ||
    lowered.startsWith(STAGE_PREFIX) ||
    lowered.startsWith(BACKUP_PREFIX) ||
    DEVICE_NAMES.has(deviceStem(part))
  );
};
export const validWriteRef = (value: unknown): value is string => {
  if (!validRef(value)) return false;
  const parts = value.split("/");
  if (parts[0]?.toLowerCase() === "bot-memory") return false;
  const leaf = parts[parts.length - 1] ?? "";
  if (!/\.md$/i.test(leaf) || /^\.+md$/i.test(leaf)) return false;
  return !parts.some(excludedWritePart);
};
const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

const MAX_METADATA = 512 * 1024;
const METADATA_REASONS = [
  "deadline",
  "byte_limit",
  "response_limit",
  "unsupported",
  "truncated",
  "changed",
  "unreadable",
] as const;
const UNREADABLE_REASONS = [
  "missing_note",
  "private_path",
  "unsafe_path",
  "note_too_large",
  "target_busy",
  "read_failed",
  "invalid_encoding",
] as const;
export const metadataRef = (value: unknown): value is string =>
  validRef(value) &&
  !/[\p{Cc}\p{Cs}]/u.test(value) &&
  !value.split("/").includes("") &&
  /[^/]\.(md|markdown)$/iu.test(value);
const exactKeys = (value: Record<string, unknown>, keys: readonly string[]): boolean =>
  Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));

export interface VaultMetadataItem {
  ref: string;
  state: DisplayMetadata["state"] | "changed" | "unreadable" | "unknown";
  reason:
    | null
    | "unsupported_metadata"
    | "prefix_limit"
    | "changed"
    | (typeof UNREADABLE_REASONS)[number]
    | "deadline"
    | "byte_limit"
    | "response_limit";
  read_bytes: number;
  display_metadata: DisplayMetadata | null;
}
export interface VaultMetadataBatch {
  items: VaultMetadataItem[];
  partial: boolean;
  partial_reasons: (typeof METADATA_REASONS)[number][];
  read_bytes: number;
  omitted: number;
}

function metadataBatch(body: Record<string, unknown>, refs: readonly string[]): VaultMetadataBatch {
  if (
    !exactKeys(body, ["items", "partial", "partial_reasons", "read_bytes", "omitted"]) ||
    !Array.isArray(body.items) ||
    body.items.length !== refs.length
  )
    throw new Error("invalid_response");
  let charged = 0;
  let omitted = 0;
  const coverage = new Set<string>();
  for (const [index, value] of body.items.entries()) {
    const item = record(value);
    if (
      !item ||
      !exactKeys(item, ["ref", "state", "reason", "read_bytes", "display_metadata"]) ||
      item.ref !== refs[index] ||
      typeof item.read_bytes !== "number" ||
      !Number.isSafeInteger(item.read_bytes) ||
      item.read_bytes < 0 ||
      item.read_bytes > 32768
    )
      throw new Error("invalid_response");
    charged += item.read_bytes;
    const projection = parseDisplayMetadata(item.display_metadata);
    if (projection) {
      const reason =
        projection.state === "unsupported"
          ? "unsupported_metadata"
          : projection.state === "truncated"
            ? "prefix_limit"
            : null;
      if (item.state !== projection.state || item.reason !== reason)
        throw new Error("invalid_response");
      if (item.state !== "ready" && item.state !== "absent") coverage.add(projection.state);
    } else {
      if (item.display_metadata !== null) throw new Error("invalid_response");
      if (item.state === "changed" && item.reason === "changed") coverage.add("changed");
      else if (
        item.state === "unreadable" &&
        typeof item.reason === "string" &&
        UNREADABLE_REASONS.some((reason) => reason === item.reason) &&
        (item.reason !== "private_path" || item.read_bytes === 0)
      )
        coverage.add("unreadable");
      else if (
        item.state === "unknown" &&
        (item.reason === "deadline" ||
          item.reason === "byte_limit" ||
          item.reason === "response_limit")
      ) {
        coverage.add(item.reason);
        omitted += 1;
      } else throw new Error("invalid_response");
    }
  }
  const reasons = METADATA_REASONS.filter((reason) => coverage.has(reason));
  if (
    body.read_bytes !== charged ||
    charged > 1024 * 1024 ||
    body.omitted !== omitted ||
    body.partial !== Boolean(reasons.length) ||
    !Array.isArray(body.partial_reasons) ||
    body.partial_reasons.length !== reasons.length ||
    body.partial_reasons.some((reason, index) => reason !== reasons[index])
  )
    throw new Error("invalid_response");
  // Count the server's canonical ASCII encoding as well as the bounded received stream.
  const ascii = JSON.stringify(body).replace(
    /[\u007f-\uffff]/g,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  if (ascii.length > MAX_METADATA) throw new Error("invalid_response");
  return body as unknown as VaultMetadataBatch;
}

// Section 4's fixed precedence: a response's reasons must appear in this order, with no repeats.
const OVERVIEW_REASONS = [
  "deadline",
  "entry_limit",
  "item_limit",
  "byte_limit",
  "response_limit",
  "depth_limit",
  "unsafe_entry",
  "unreadable",
  "changed",
] as const;
const OVERVIEW_ROW_KEYS = [
  "ref",
  "state",
  "title",
  "type",
  "status",
  "confidence",
  "tags",
  "created",
  "modified",
  "summary",
  "links",
  "complete",
];
const OVERVIEW_KEYS = ["notes", "partial", "partial_reasons", "omitted", "read_bytes"];
const OVERVIEW_STATES = ["ready", "absent", "unsupported", "truncated"];
const OVERVIEW_DATE = /^\d{4}-\d{2}-\d{2}$/;
const overviewText = (value: unknown, maximum: number): value is string =>
  typeof value === "string" && [...value].length <= maximum;

export interface OverviewNote {
  ref: string;
  state: DisplayMetadata["state"];
  title: string | null;
  type: string | null;
  status: string | null;
  confidence: string | null;
  tags: string[] | null;
  created: string | null;
  modified: string | null;
  summary: string | null;
  links: number | null;
  complete: boolean;
}
export interface VaultOverview {
  notes: OverviewNote[];
  partial: boolean;
  partial_reasons: (typeof OVERVIEW_REASONS)[number][];
  omitted: number;
  read_bytes: number;
}

function parseOverviewNote(value: unknown): OverviewNote {
  const item = record(value);
  if (!item || !exactKeys(item, OVERVIEW_ROW_KEYS)) throw new Error("invalid_response");
  if (!metadataRef(item.ref)) throw new Error("invalid_response");
  if (typeof item.state !== "string" || !OVERVIEW_STATES.includes(item.state))
    throw new Error("invalid_response");
  if (item.title !== null && !overviewText(item.title, 512)) throw new Error("invalid_response");
  if (item.type !== null && !overviewText(item.type, 64)) throw new Error("invalid_response");
  if (item.status !== null && !overviewText(item.status, 64)) throw new Error("invalid_response");
  if (item.confidence !== null && !overviewText(item.confidence, 64))
    throw new Error("invalid_response");
  const tags = item.tags;
  if (
    tags !== null &&
    (!Array.isArray(tags) ||
      tags.length > 16 ||
      !tags.every((tag) => overviewText(tag, 64)) ||
      new Set(tags).size !== tags.length)
  )
    throw new Error("invalid_response");
  if (
    item.created !== null &&
    (typeof item.created !== "string" || !OVERVIEW_DATE.test(item.created))
  )
    throw new Error("invalid_response");
  if (
    item.modified !== null &&
    (typeof item.modified !== "string" || !OVERVIEW_DATE.test(item.modified))
  )
    throw new Error("invalid_response");
  if (
    item.summary !== null &&
    (typeof item.summary !== "string" ||
      [...item.summary].length > 201 ||
      /[\p{Cc}\p{Cs}]/u.test(item.summary))
  )
    throw new Error("invalid_response");
  if (
    item.links !== null &&
    (typeof item.links !== "number" ||
      !Number.isSafeInteger(item.links) ||
      item.links < 0 ||
      item.links > 256)
  )
    throw new Error("invalid_response");
  if (typeof item.complete !== "boolean") throw new Error("invalid_response");
  return item as unknown as OverviewNote;
}

// Parses the `GET .../overview` envelope: same untrusted-response posture as
// `metadataBatch` above, including the exact-keys and canonical-order checks.
function parseOverview(raw: unknown): VaultOverview {
  const body = record(raw);
  if (
    !body ||
    !exactKeys(body, OVERVIEW_KEYS) ||
    !Array.isArray(body.notes) ||
    body.notes.length > 2000
  )
    throw new Error("invalid_response");
  const notes = body.notes.map(parseOverviewNote);
  if (new Set(notes.map((note) => note.ref)).size !== notes.length)
    throw new Error("invalid_response");
  if (!Array.isArray(body.partial_reasons)) throw new Error("invalid_response");
  let lastIndex = -1;
  for (const reason of body.partial_reasons) {
    const index = OVERVIEW_REASONS.findIndex((candidate) => candidate === reason);
    if (index <= lastIndex) throw new Error("invalid_response");
    lastIndex = index;
  }
  if (
    typeof body.partial !== "boolean" ||
    body.partial !== Boolean(body.partial_reasons.length) ||
    typeof body.omitted !== "number" ||
    !Number.isSafeInteger(body.omitted) ||
    body.omitted < 0 ||
    typeof body.read_bytes !== "number" ||
    !Number.isSafeInteger(body.read_bytes) ||
    body.read_bytes < 0
  )
    throw new Error("invalid_response");
  return { ...body, notes } as unknown as VaultOverview;
}

export interface VaultEntry {
  ref: string;
  title: string;
  bytes: number;
  modified: number;
}
export interface VaultNote {
  ref: string;
  title: string;
  text: string;
  source: string;
  byte_size: number;
  sha256: string;
  frontmatter: Record<string, string>;
  links: string[];
  frontmatter_error: string | null;
  display_metadata?: DisplayMetadata;
}
export interface VaultSaveOutcome {
  operation_id: string;
  status: "saved" | "no_change" | "conflict";
  cleanup_pending: boolean;
}
export type InstructionName = "AGENTS.md" | "CLAUDE.md";
export interface InstructionSaveOutcome extends VaultSaveOutcome {
  sha256: string | null;
}
export interface VaultRecoveryStatus {
  operation_id: string;
  workspace_id: string;
  ref: string;
  state: "reserved" | "staged" | "prepared" | "backed_up" | "published" | "conflict";
  reason: "conflict" | "recovery_required";
  resolvable: readonly ("forget" | "drop_previous")[];
}
export interface VaultBookmark {
  ref: string;
  created_at: string;
}
export interface VaultCommit {
  short: string;
  at: string;
  subject: string;
}
export interface VaultNoteHistory {
  commits: VaultCommit[];
  state: "ready" | "unknown";
}
export class VaultWriteError extends Error {
  constructor(
    readonly code: string,
    readonly operationId: string | null = null,
  ) {
    super(code);
  }
}
export interface VaultApi {
  metadata?(id: string, refs: readonly string[], signal?: AbortSignal): Promise<VaultMetadataBatch>;
  overview?(id: string, signal?: AbortSignal): Promise<VaultOverview>;
  entries(id: string, q?: string, signal?: AbortSignal): Promise<Record<string, unknown>>;
  note(id: string, ref: string, signal?: AbortSignal): Promise<Record<string, unknown>>;
  backlinks(id: string, ref: string, signal?: AbortSignal): Promise<Record<string, unknown>>;
  history(id: string, ref: string, signal?: AbortSignal): Promise<Record<string, unknown>>;
  bookmarks(id: string, signal?: AbortSignal): Promise<Record<string, unknown>>;
  addBookmark(id: string, ref: string, signal?: AbortSignal): Promise<Record<string, unknown>>;
  removeBookmark(id: string, ref: string, signal?: AbortSignal): Promise<void>;
  createNote(
    id: string,
    policyRevision: number,
    rootPhysicalId: string,
    ref: string,
    content: string,
    signal?: AbortSignal,
  ): Promise<VaultSaveOutcome>;
  saveNote(
    id: string,
    policyRevision: number,
    rootPhysicalId: string,
    ref: string,
    content: string,
    expectedSha256: string,
    signal?: AbortSignal,
  ): Promise<VaultSaveOutcome>;
  saveInstruction?(
    id: string,
    policyRevision: number,
    rootPhysicalId: string,
    name: InstructionName,
    content: string,
    expectedSha256: string,
    signal?: AbortSignal,
  ): Promise<InstructionSaveOutcome>;
  recovery(signal?: AbortSignal): Promise<VaultRecoveryStatus[]>;
}
export type VaultReadApi = Pick<
  VaultApi,
  "entries" | "note" | "backlinks" | "history" | "metadata" | "overview"
>;
export type VaultBookmarkApi = Pick<VaultApi, "bookmarks" | "addBookmark" | "removeBookmark">;
export type VaultWriteApi = Pick<VaultApi, "createNote" | "saveNote" | "recovery">;

// Required: a server that omits the list must fail loudly, not hide the resolve buttons.
function validResolvable(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.length <= 2 &&
    value.every((choice) => choice === "forget" || choice === "drop_previous") &&
    new Set(value).size === value.length
  );
}

export function createVaultApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
  base = "/api/v2/vaults",
): VaultApi {
  async function get(
    id: string,
    route: string,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    if (!UUID.test(id)) throw new Error("invalid_workspace");
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT);
    try {
      const response = await fetchImpl(`${base}/${id}/${route}`, {
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        if (response.status === 403) throw new Error("forbidden");
        if (response.status === 404) throw new Error("not_found");
        // Carry the server's error.code so the UI can show code-specific copy.
        let code = "unavailable";
        try {
          const body = record(await boundedJson(response, MAX));
          const error = body ? record(body.error) : null;
          if (error && typeof error.code === "string") code = error.code;
        } catch {
          // The body was not a readable JSON error; keep the generic code.
        }
        throw new Error(code);
      }
      const body = record(await boundedJson(response, MAX));
      if (!body) throw new Error("invalid_response");
      return body;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  async function request(
    route: string,
    init: RequestInit,
    maximum: number,
    signal?: AbortSignal,
  ): Promise<{ body: Record<string, unknown>; ok: boolean; status: number }> {
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT);
    try {
      const response = await fetchImpl(route, {
        ...init,
        signal: controller.signal,
        headers: { authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
      });
      const body = record(await boundedJson(response, maximum));
      if (!body) throw new Error("invalid_response");
      return { body, ok: response.ok, status: response.status };
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  // PUT and DELETE carry no body and no content-type: the server answers 415 to either.
  async function mark(
    method: "PUT" | "DELETE",
    id: string,
    ref: string,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    if (!UUID.test(id)) throw new Error("invalid_workspace");
    if (!validRef(ref)) throw new Error("invalid_ref");
    const result = await request(
      `${base}/${id}/bookmarks/${encodeURIComponent(ref).replace(/%2F/g, "/")}`,
      { method },
      8192,
      signal,
    );
    if (!result.ok) {
      const error = record(result.body.error);
      throw new Error(error && typeof error.code === "string" ? error.code : "unavailable");
    }
    return result.body;
  }
  const validBody = (id: string, policyRevision: number, rootPhysicalId: string, content: string) =>
    UUID.test(id) &&
    Number.isSafeInteger(policyRevision) &&
    policyRevision >= 1 &&
    PHYSICAL_ID.test(rootPhysicalId) &&
    rootPhysicalId.length <= 80 &&
    content.isWellFormed() &&
    !Array.from(content).some((character) => {
      const code = character.codePointAt(0) ?? 0;
      return code < 32 && character !== "\t" && character !== "\n" && character !== "\r";
    }) &&
    new TextEncoder().encode(content).byteLength <= MAX_WRITE;
  const validWrite = (
    id: string,
    policyRevision: number,
    rootPhysicalId: string,
    ref: string,
    content: string,
  ) => validBody(id, policyRevision, rootPhysicalId, content) && validWriteRef(ref);

  function saveOutcome(result: {
    body: Record<string, unknown>;
    ok: boolean;
    status: number;
  }): VaultSaveOutcome {
    const operationId = result.body.operation_id;
    const status = result.body.status;
    if (
      typeof operationId === "string" &&
      UUID.test(operationId) &&
      (status === "saved" || status === "no_change" || status === "conflict") &&
      typeof result.body.cleanup_pending === "boolean" &&
      (result.ok || (result.status === 409 && status === "conflict"))
    )
      return result.body as unknown as VaultSaveOutcome;
    const error = record(result.body.error);
    if (error && typeof error.code === "string") {
      const failedOperation =
        typeof error.operation_id === "string" && UUID.test(error.operation_id)
          ? error.operation_id
          : null;
      throw new VaultWriteError(error.code, failedOperation);
    }
    throw new VaultWriteError("invalid_response");
  }
  async function write(
    id: string,
    policyRevision: number,
    rootPhysicalId: string,
    ref: string,
    content: string,
    expectedSha256: string | null,
    signal?: AbortSignal,
  ): Promise<VaultSaveOutcome> {
    if (
      !validWrite(id, policyRevision, rootPhysicalId, ref, content) ||
      (expectedSha256 !== null && !SHA256.test(expectedSha256))
    )
      throw new VaultWriteError("invalid_publication");
    const route = `${base}/${id}/notes/${encodeURIComponent(ref).replace(/%2F/g, "/")}`;
    const result = await request(
      route,
      {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          policy_revision: policyRevision,
          root_physical_id: rootPhysicalId,
          content,
          expected_sha256: expectedSha256,
        }),
      },
      8192,
      signal,
    );
    return saveOutcome(result);
  }
  return {
    async metadata(id, refs, signal) {
      if (!UUID.test(id)) throw new Error("invalid_workspace");
      if (
        !Array.isArray(refs) ||
        refs.length < 1 ||
        refs.length > 32 ||
        !refs.every(metadataRef) ||
        new Set(refs).size !== refs.length
      )
        throw new Error("invalid_metadata_request");
      const requested = [...refs];
      const result = await request(
        `${base}/${id}/metadata`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ refs: requested }),
        },
        MAX_METADATA,
        signal,
      );
      if (!result.ok) {
        const error = record(result.body.error);
        throw new Error(error && typeof error.code === "string" ? error.code : "unavailable");
      }
      return metadataBatch(result.body, requested);
    },
    async overview(id, signal) {
      return parseOverview(await get(id, "overview", signal));
    },
    async entries(id, q, signal) {
      if (
        q !== undefined &&
        (!q.isWellFormed() ||
          q.length > 256 ||
          new TextEncoder().encode(`q=${encodeURIComponent(q)}`).byteLength > 1024)
      )
        throw new Error("invalid_query");
      const body = await get(
        id,
        `entries${q !== undefined ? `?q=${encodeURIComponent(q)}` : ""}`,
        signal,
      );
      const rows = Array.isArray(body.entries)
        ? body.entries
        : Array.isArray(body.hits)
          ? body.hits
          : null;
      if (
        !rows ||
        rows.length > (q === undefined ? 5000 : 100) ||
        rows.some((row) => {
          const item = record(row);
          return !item || !validRef(item.ref);
        })
      )
        throw new Error("invalid_response");
      return body;
    },
    async note(id, ref, signal) {
      if (!validRef(ref)) throw new Error("invalid_ref");
      const body = await get(id, `notes/${encodeURIComponent(ref).replace(/%2F/g, "/")}`, signal);
      const note = record(body.note);
      if (
        !note ||
        !validRef(note.ref) ||
        typeof note.text !== "string" ||
        !Array.isArray(body.resolved_links) ||
        (Object.hasOwn(note, "display_metadata") &&
          parseDisplayMetadata(note.display_metadata) === null)
      )
        throw new Error("invalid_response");
      return body;
    },
    async backlinks(id, ref, signal) {
      if (!validRef(ref)) throw new Error("invalid_ref");
      const body = await get(
        id,
        `backlinks/${encodeURIComponent(ref).replace(/%2F/g, "/")}`,
        signal,
      );
      if (!Array.isArray(body.backlinks)) throw new Error("invalid_response");
      return body;
    },
    async history(id, ref, signal) {
      if (!validRef(ref)) throw new Error("invalid_ref");
      return get(id, `history/${encodeURIComponent(ref).replace(/%2F/g, "/")}`, signal);
    },
    bookmarks(id, signal) {
      return get(id, "bookmarks", signal);
    },
    addBookmark(id, ref, signal) {
      return mark("PUT", id, ref, signal);
    },
    async removeBookmark(id, ref, signal) {
      await mark("DELETE", id, ref, signal);
    },
    createNote(id, policyRevision, rootPhysicalId, ref, content, signal) {
      return write(id, policyRevision, rootPhysicalId, ref, content, null, signal);
    },
    saveNote(id, policyRevision, rootPhysicalId, ref, content, expectedSha256, signal) {
      return write(id, policyRevision, rootPhysicalId, ref, content, expectedSha256, signal);
    },
    async saveInstruction(
      id,
      policyRevision,
      rootPhysicalId,
      name,
      content,
      expectedSha256,
      signal,
    ) {
      if (
        !validBody(id, policyRevision, rootPhysicalId, content) ||
        (name !== "AGENTS.md" && name !== "CLAUDE.md") ||
        !SHA256.test(expectedSha256)
      )
        throw new VaultWriteError("invalid_publication");
      const result = await request(
        `${base}/${id}/instructions/${name}`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            policy_revision: policyRevision,
            root_physical_id: rootPhysicalId,
            content,
            expected_sha256: expectedSha256,
          }),
        },
        8192,
        signal,
      );
      const outcome = saveOutcome(result);
      const sha256 = result.body.sha256;
      if (
        outcome.status === "conflict"
          ? sha256 !== null
          : typeof sha256 !== "string" || !SHA256.test(sha256)
      )
        throw new VaultWriteError("invalid_response");
      return { ...outcome, sha256: sha256 as string | null };
    },
    async recovery(signal) {
      const result = await request(
        "/api/v2/vault-publications/recovery",
        { method: "GET" },
        128 * 1024,
        signal,
      );
      const rows = result.body.publications;
      if (
        !result.ok ||
        !Array.isArray(rows) ||
        rows.length > 128 ||
        rows.some((row) => {
          const item = record(row);
          return (
            !item ||
            typeof item.operation_id !== "string" ||
            !UUID.test(item.operation_id) ||
            typeof item.workspace_id !== "string" ||
            !UUID.test(item.workspace_id) ||
            !validRef(item.ref) ||
            !["reserved", "staged", "prepared", "backed_up", "published", "conflict"].includes(
              String(item.state),
            ) ||
            (item.reason !== "conflict" && item.reason !== "recovery_required") ||
            !validResolvable(item.resolvable)
          );
        })
      )
        throw new VaultWriteError("invalid_response");
      return rows as VaultRecoveryStatus[];
    },
  };
}
