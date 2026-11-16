import { boundedJson } from "../clients/boundedResponse";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PHYSICAL_ID = /^[0-9]+:[0-9]+$/;
const HASH = /^[0-9a-f]{64}$/;
// Exactly `attachments/<leaf>`: one fixed parent, one component, no drive or separator tricks.
const ATTACHMENT_REF = /^attachments\/[^/\\:]{1,255}$/;
// A drive path, a UNC path or a rooted path: the server refuses a relative one.
const ABSOLUTE_PATH = /^(?:[A-Za-z]:[\\/]|\\\\|\/)/;
const MAX = 256 * 1024;
const MAX_LIST = 4 * 1024 * 1024;
const TIMEOUT = 60_000;
const KINDS = ["owner", "assistant", "inference", "summary"] as const;
const SCOPES = ["vault", "personal"] as const;
const STATUSES = ["candidate", "approved", "retired"] as const;
const STATES = ["retired", "active", "pending", "candidate"] as const;

export interface KeepAttachmentRequest {
  policyRevision: number;
  rootPhysicalId: string;
  assetId: string;
  name: string;
}
export interface KeptAttachment {
  ref: string;
  reused: boolean;
  byte_size: number;
}
export interface BackupSkip {
  ref: string;
  reason: string;
}
export interface BackupPreview {
  files: number;
  bytes: number;
  instruction_files: string[];
  instruction_files_total: number;
  skipped: BackupSkip[];
  skipped_total: number;
  local_recovery: boolean;
}
export interface BackupResult {
  folder: string;
  files: number;
  bytes: number;
  skipped: BackupSkip[];
  skipped_total: number;
  replayed: boolean;
}
export type MemoryKind = (typeof KINDS)[number];
export type MemoryScope = (typeof SCOPES)[number];
export type MemoryStatus = (typeof STATUSES)[number];
export type MemoryState = (typeof STATES)[number];
export type MemorySource = { note: string; sha256: string } | { session: string };
export interface MemoryFields {
  id: string;
  kind: MemoryKind;
  scope: MemoryScope;
  status: MemoryStatus;
  created: string;
  reviewed: string | null;
  creator: string | null;
  sources: MemorySource[];
  supersedes: string | null;
  retired_by: string | null;
}
export interface MemoryItem {
  ref: string;
  sha256: string;
  state: MemoryState;
  fields: MemoryFields;
  claim: string;
  superseded_by: string | null;
}
/** A file under `memory/` that is not a usable record; `text` is null when the reader refused. */
export interface InvalidMemory {
  ref: string;
  reason: string;
  text: string | null;
  state: "invalid";
}
export interface MemoryListing {
  items: MemoryItem[];
  invalid: InvalidMemory[];
  partial: boolean;
}
export interface MemoryDraft {
  kind: MemoryKind;
  scope: MemoryScope;
  claim: string;
  sources: MemorySource[];
}
export interface CreateMemoryRequest extends MemoryDraft {
  policyRevision: number;
  rootPhysicalId: string;
}
export interface MemoryWritten {
  outcome: "written";
  memory_id: string;
  ref: string;
  sha256: string;
  operation_id: string;
  created_folders: string[];
}
/** A protected vault turns a create into a proposal for the user: nothing was written. */
export interface MemoryProposed {
  outcome: "proposed";
  memory_id: string;
  ref: string;
  proposal_id: string;
  proposed_sha256: string;
}
/** A replayed create wrote nothing, so it names no operation: `operation_id` is null then. */
export type CreatedMemory =
  | (Omit<MemoryWritten, "operation_id"> & { operation_id: string | null; replayed: boolean })
  | (MemoryProposed & { replayed: boolean });
export interface MemoryReviewed {
  outcome: "reviewed";
  memory_id: string;
  sha256: string;
}
export interface MemoryCorrected {
  outcome: "corrected";
  record: MemoryWritten;
  retired: MemoryWritten;
}
interface MemoryPatchBase {
  policyRevision: number;
  rootPhysicalId: string;
  sha256: string;
}
export type MemoryPatch =
  | (MemoryPatchBase & { action: "approve" | "narrow_scope" })
  | (MemoryPatchBase & { action: "retire"; retiredBy: string | null })
  | (MemoryPatchBase & { action: "correct"; claim: string });
export type PatchedMemory = MemoryReviewed | MemoryWritten | MemoryCorrected;
export interface RestorePreview {
  source_name: string;
  source_policy: string;
  protected: boolean;
  files: number;
  bytes: number;
  instruction_files: string[];
  instruction_files_total: number;
  problems: BackupSkip[];
  problems_total: number;
  backup_skipped: number;
}
export interface RestoreRequest {
  backup: string;
  parent: string;
  folder: string;
}
export interface RestoreResult {
  workspace: { workspace_id: string; name: string; policy: string };
  files: number;
  bytes: number;
  verified: boolean;
  problems: BackupSkip[];
  problems_total: number;
  replayed: boolean;
}
/** The server's typed code, or "unavailable" when the answer carries none. Never its message. */
export class KnowledgeError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export interface VaultKnowledgeApi {
  keepAttachment(
    workspaceId: string,
    request: KeepAttachmentRequest,
    commandId: string,
    signal?: AbortSignal,
  ): Promise<KeptAttachment>;
  backupPreview(
    workspaceId: string,
    destination: string,
    signal?: AbortSignal,
  ): Promise<BackupPreview>;
  backup(
    workspaceId: string,
    destination: string,
    commandId: string,
    signal?: AbortSignal,
  ): Promise<BackupResult>;
  memories(workspaceId: string, signal?: AbortSignal): Promise<MemoryListing>;
  createMemory(
    workspaceId: string,
    request: CreateMemoryRequest,
    commandId: string,
    signal?: AbortSignal,
  ): Promise<CreatedMemory>;
  composeMemory(
    workspaceId: string,
    draft: MemoryDraft,
    signal?: AbortSignal,
  ): Promise<{ text: string }>;
  patchMemory(
    workspaceId: string,
    memoryId: string,
    request: MemoryPatch,
    signal?: AbortSignal,
  ): Promise<PatchedMemory>;
  /** Restores a backup into a fresh folder; not the journal's `restore` kind. */
  backupRestorePreview(backup: string, signal?: AbortSignal): Promise<RestorePreview>;
  backupRestore(
    commandId: string,
    request: RestoreRequest,
    signal?: AbortSignal,
  ): Promise<RestoreResult>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const uuid = (value: unknown): value is string => typeof value === "string" && UUID.test(value);
const hash = (value: unknown): value is string => typeof value === "string" && HASH.test(value);
const count = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const text = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0 && value.length <= 512;
// Backup note refs use the server's code-point limit, including instruction and skipped refs.
const refText = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0 && Array.from(value).length <= 512;
const unavailable = () => new KnowledgeError("unavailable");
const nullable = (check: (value: unknown) => boolean, value: unknown) =>
  value === null || check(value);
const oneOf = <T extends string>(values: readonly T[], value: unknown): value is T =>
  typeof value === "string" && (values as readonly string[]).includes(value);
const hasCode = (value: string, allowed: (code: number) => boolean) =>
  Array.from(value).some((character) => allowed(character.codePointAt(0) ?? 0));
const validSkips = (value: unknown): value is BackupSkip[] =>
  Array.isArray(value) &&
  value.length <= 2_000 &&
  value.every((row) => {
    const skip = record(row);
    return !!skip && refText(skip.ref) && text(skip.reason);
  });
function diagnosticTotal(body: Record<string, unknown>, key: string, shown: number): number {
  if (!(key in body)) return shown; // legacy servers sent complete lists
  const total = body[key];
  if (!count(total) || total < shown) throw unavailable();
  return total;
}
// An absolute path of 1..4096 characters with no control code, as the server's path fields.
const validDestination = (value: unknown): value is string =>
  typeof value === "string" &&
  value.length > 0 &&
  value.length <= 4096 &&
  ABSOLUTE_PATH.test(value) &&
  !hasCode(value, (code) => code < 32 || code === 127);
const validFolder = (value: unknown): value is string =>
  typeof value === "string" &&
  value.length > 0 &&
  value.length <= 255 &&
  !hasCode(value, (code) => code < 32 || code === 127);
// Every C0 control but tab, LF and CR is refused, as the server's claim check; its byte bound and
// grammar stay the server's.
const validClaim = (value: unknown): value is string =>
  typeof value === "string" &&
  value.length <= 16_384 &&
  !hasCode(
    value,
    (code) => (code < 32 && code !== 9 && code !== 10 && code !== 13) || code === 127,
  );
const validSource = (value: unknown): value is MemorySource => {
  const source = record(value);
  if (!source) return false;
  const keys = Object.keys(source).sort().join();
  if (keys === "note,sha256")
    return typeof source.note === "string" && source.note.length > 0 && hash(source.sha256);
  return keys === "session" && uuid(source.session);
};
const validSources = (value: unknown): value is MemorySource[] =>
  Array.isArray(value) && value.length <= 16 && value.every(validSource);
const nullableString = (value: unknown) => nullable((row) => typeof row === "string", value);
const validFields = (value: unknown): value is MemoryFields => {
  const fields = record(value);
  return (
    !!fields &&
    uuid(fields.id) &&
    oneOf(KINDS, fields.kind) &&
    oneOf(SCOPES, fields.scope) &&
    oneOf(STATUSES, fields.status) &&
    typeof fields.created === "string" &&
    nullableString(fields.reviewed) &&
    nullableString(fields.creator) &&
    validSources(fields.sources) &&
    nullable(uuid, fields.supersedes) &&
    nullable(uuid, fields.retired_by)
  );
};
const validItem = (value: unknown): value is MemoryItem => {
  const item = record(value);
  return (
    !!item &&
    text(item.ref) &&
    hash(item.sha256) &&
    oneOf(STATES, item.state) &&
    validFields(item.fields) &&
    typeof item.claim === "string" &&
    nullable(uuid, item.superseded_by)
  );
};
const validInvalid = (value: unknown): value is InvalidMemory => {
  const item = record(value);
  return (
    !!item &&
    text(item.ref) &&
    text(item.reason) &&
    nullableString(item.text) &&
    item.state === "invalid"
  );
};
const rows = <T>(value: unknown, check: (row: unknown) => row is T): value is T[] =>
  Array.isArray(value) && value.length <= 2_000 && value.every(check);
const validWritten = (value: unknown, replayed = false): value is MemoryWritten => {
  const body = record(value);
  return (
    !!body &&
    body.outcome === "written" &&
    uuid(body.memory_id) &&
    text(body.ref) &&
    hash(body.sha256) &&
    (text(body.operation_id) || (replayed && body.operation_id === null)) &&
    rows(body.created_folders, text)
  );
};
const validProposed = (value: unknown): value is MemoryProposed => {
  const body = record(value);
  return (
    !!body &&
    body.outcome === "proposed" &&
    uuid(body.memory_id) &&
    text(body.ref) &&
    text(body.proposal_id) &&
    hash(body.proposed_sha256)
  );
};
const validReviewed = (value: unknown): value is MemoryReviewed => {
  const body = record(value);
  return !!body && body.outcome === "reviewed" && uuid(body.memory_id) && hash(body.sha256);
};
const validCorrected = (value: unknown): value is MemoryCorrected => {
  const body = record(value);
  return (
    !!body &&
    body.outcome === "corrected" &&
    validWritten(body.record) &&
    validWritten(body.retired)
  );
};

export function createVaultKnowledgeApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): VaultKnowledgeApi {
  // Returns the parsed body; a 200 and a 201 are both a success, any other status is an error.
  async function call(
    method: "GET" | "POST" | "PATCH",
    route: string,
    body: unknown,
    signal?: AbortSignal,
    max = MAX,
  ): Promise<Record<string, unknown>> {
    if (signal?.aborted) throw new Error("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT);
    try {
      const response = await fetchImpl(route, {
        method,
        signal: controller.signal,
        headers:
          method === "GET"
            ? { authorization: `Bearer ${token}` }
            : { authorization: `Bearer ${token}`, "content-type": "application/json" },
        ...(method === "GET" ? {} : { body: JSON.stringify(body) }),
      });
      const answer = record(await boundedJson(response, max).catch(() => null));
      if ((response.status === 200 || response.status === 201) && answer) return answer;
      // Typed codes ride 403, 404, 409, 413, 423, 429 and 503; act on the code only.
      const error = record(answer?.error);
      throw error && text(error.code) ? new KnowledgeError(error.code) : unavailable();
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const post = (route: string, body: unknown, signal?: AbortSignal) =>
    call("POST", route, body, signal);
  const memoriesRoute = (workspaceId: string) => {
    if (!uuid(workspaceId)) throw new KnowledgeError("invalid_workspace");
    return `/api/v2/vaults/${workspaceId}/memories`;
  };
  const admission = (policyRevision: number, rootPhysicalId: string) => {
    if (
      !Number.isSafeInteger(policyRevision) ||
      policyRevision < 1 ||
      typeof rootPhysicalId !== "string" ||
      !PHYSICAL_ID.test(rootPhysicalId) ||
      rootPhysicalId.length > 80
    )
      throw new KnowledgeError("invalid_request");
    return { policy_revision: policyRevision, root_physical_id: rootPhysicalId };
  };
  const draftBody = (draft: MemoryDraft) => {
    if (
      !oneOf(KINDS, draft.kind) ||
      !oneOf(SCOPES, draft.scope) ||
      !validClaim(draft.claim) ||
      !validSources(draft.sources)
    )
      throw new KnowledgeError("invalid_request");
    return { kind: draft.kind, scope: draft.scope, claim: draft.claim, sources: draft.sources };
  };
  const backupBody = (workspaceId: string, destination: string) => {
    if (!uuid(workspaceId)) throw new KnowledgeError("invalid_workspace");
    if (!validDestination(destination)) throw new KnowledgeError("invalid_path");
    return { destination };
  };
  return {
    async keepAttachment(workspaceId, request, commandId, signal) {
      const { policyRevision, rootPhysicalId, assetId, name } = request;
      if (!uuid(workspaceId)) throw new KnowledgeError("invalid_workspace");
      if (!uuid(commandId)) throw new KnowledgeError("invalid_command");
      if (
        !Number.isSafeInteger(policyRevision) ||
        policyRevision < 1 ||
        !PHYSICAL_ID.test(rootPhysicalId) ||
        rootPhysicalId.length > 80 ||
        !uuid(assetId) ||
        typeof name !== "string" ||
        name.length > 255
      )
        throw new KnowledgeError("invalid_request");
      const body = await post(
        `/api/v2/vaults/${workspaceId}/attachments`,
        {
          command_id: commandId,
          policy_revision: policyRevision,
          root_physical_id: rootPhysicalId,
          asset_id: assetId,
          name,
        },
        signal,
      );
      if (
        typeof body.ref !== "string" ||
        !ATTACHMENT_REF.test(body.ref) ||
        Array.from(body.ref).some((character) => (character.codePointAt(0) ?? 0) < 32) ||
        typeof body.reused !== "boolean" ||
        !count(body.byte_size)
      )
        throw unavailable();
      return body as unknown as KeptAttachment;
    },
    async backupPreview(workspaceId, destination, signal) {
      const body = await post(
        `/api/v2/vaults/${workspaceId}/backup/preview`,
        backupBody(workspaceId, destination),
        signal,
      );
      if (
        !count(body.files) ||
        !count(body.bytes) ||
        !Array.isArray(body.instruction_files) ||
        body.instruction_files.length > 2_000 ||
        !body.instruction_files.every(refText) ||
        !validSkips(body.skipped) ||
        typeof body.local_recovery !== "boolean"
      )
        throw unavailable();
      return {
        ...body,
        instruction_files_total: diagnosticTotal(
          body,
          "instruction_files_total",
          body.instruction_files.length,
        ),
        skipped_total: diagnosticTotal(body, "skipped_total", body.skipped.length),
      } as unknown as BackupPreview;
    },
    async backup(workspaceId, destination, commandId, signal) {
      const request = backupBody(workspaceId, destination);
      if (!uuid(commandId)) throw new KnowledgeError("invalid_command");
      const body = await post(
        `/api/v2/vaults/${workspaceId}/backup`,
        { command_id: commandId, ...request },
        signal,
      );
      if (
        !text(body.folder) ||
        !count(body.files) ||
        !count(body.bytes) ||
        !validSkips(body.skipped) ||
        typeof body.replayed !== "boolean"
      )
        throw unavailable();
      return {
        ...body,
        skipped_total: diagnosticTotal(body, "skipped_total", body.skipped.length),
      } as unknown as BackupResult;
    },
    async memories(workspaceId, signal) {
      const body = await call("GET", memoriesRoute(workspaceId), undefined, signal, MAX_LIST);
      if (
        !rows(body.items, validItem) ||
        !rows(body.invalid, validInvalid) ||
        typeof body.partial !== "boolean"
      )
        throw unavailable();
      return body as unknown as MemoryListing;
    },
    async createMemory(workspaceId, request, commandId, signal) {
      const route = memoriesRoute(workspaceId);
      if (!uuid(commandId)) throw new KnowledgeError("invalid_command");
      const body = await post(
        route,
        {
          command_id: commandId,
          ...admission(request.policyRevision, request.rootPhysicalId),
          ...draftBody(request),
        },
        signal,
      );
      const { replayed, ...outcome } = body;
      if (
        typeof replayed !== "boolean" ||
        !(validWritten(outcome, replayed) || validProposed(outcome))
      )
        throw unavailable();
      return body as unknown as CreatedMemory;
    },
    async composeMemory(workspaceId, draft, signal) {
      const route = memoriesRoute(workspaceId);
      const body = await post(`${route}/compose`, draftBody(draft), signal);
      if (typeof body.text !== "string") throw unavailable();
      return body as unknown as { text: string };
    },
    async patchMemory(workspaceId, memoryId, request, signal) {
      const route = memoriesRoute(workspaceId);
      if (!uuid(memoryId)) throw new KnowledgeError("invalid_memory");
      const common = admission(request.policyRevision, request.rootPhysicalId);
      if (!hash(request.sha256)) throw new KnowledgeError("invalid_request");
      let extra: Record<string, unknown>;
      switch (request.action) {
        case "approve":
        case "narrow_scope":
          extra = {};
          break;
        case "retire":
          if (request.retiredBy !== null && !uuid(request.retiredBy))
            throw new KnowledgeError("invalid_request");
          extra = { retired_by: request.retiredBy };
          break;
        case "correct":
          if (!validClaim(request.claim)) throw new KnowledgeError("invalid_request");
          extra = { claim: request.claim };
          break;
        default:
          throw new KnowledgeError("invalid_request");
      }
      const body = await call(
        "PATCH",
        `${route}/${memoryId}`,
        { ...common, action: request.action, sha256: request.sha256, ...extra },
        signal,
      );
      const ok =
        request.action === "correct"
          ? validCorrected(body)
          : validWritten(body) || (request.action === "approve" && validReviewed(body));
      if (!ok) throw unavailable();
      return body as unknown as PatchedMemory;
    },
    async backupRestorePreview(backup, signal) {
      if (!validDestination(backup)) throw new KnowledgeError("invalid_path");
      const body = await post("/api/v2/backup-restore/preview", { backup }, signal);
      if (
        typeof body.source_name !== "string" ||
        typeof body.source_policy !== "string" ||
        typeof body.protected !== "boolean" ||
        !count(body.files) ||
        !count(body.bytes) ||
        !Array.isArray(body.instruction_files) ||
        body.instruction_files.length > 2_000 ||
        !body.instruction_files.every(refText) ||
        !validSkips(body.problems) ||
        !count(body.backup_skipped)
      )
        throw unavailable();
      return {
        ...body,
        instruction_files_total: diagnosticTotal(
          body,
          "instruction_files_total",
          body.instruction_files.length,
        ),
        problems_total: diagnosticTotal(body, "problems_total", body.problems.length),
      } as unknown as RestorePreview;
    },
    async backupRestore(commandId, request, signal) {
      if (!uuid(commandId)) throw new KnowledgeError("invalid_command");
      const { backup, parent, folder } = request;
      if (!validDestination(backup) || !validDestination(parent))
        throw new KnowledgeError("invalid_path");
      if (!validFolder(folder)) throw new KnowledgeError("invalid_name");
      const body = await post(
        "/api/v2/backup-restore",
        { command_id: commandId, backup, parent, folder },
        signal,
      );
      const workspace = record(body.workspace);
      if (
        !workspace ||
        !uuid(workspace.workspace_id) ||
        typeof workspace.name !== "string" ||
        typeof workspace.policy !== "string" ||
        !count(body.files) ||
        !count(body.bytes) ||
        typeof body.verified !== "boolean" ||
        !validSkips(body.problems) ||
        typeof body.replayed !== "boolean"
      )
        throw unavailable();
      return {
        ...body,
        problems_total: diagnosticTotal(body, "problems_total", body.problems.length),
      } as unknown as RestoreResult;
    },
  };
}
