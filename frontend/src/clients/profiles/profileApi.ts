/**
 * Typed client for the /api/v2/profiles* routes.
 *
 * Bearer auth, 15 s composed abort (timeout vs caller cancel), 2 MiB response cap via
 * boundedJson. Caller input is validated before fetch; every response is field-validated
 * before it is returned. No retries, no caching, no logging.
 */
import { boundedJson } from "../boundedResponse";

export const PROFILE_ERROR_CODES = [
  "invalid_input",
  "invalid_revision",
  "invalid_id",
  "not_found",
  "conflict",
  "command_conflict",
  "capacity",
  "newer_version",
  "invalid_document",
  "unavailable",
  "too_large",
  "overloaded",
  "intake_timeout",
  "disconnected",
  "unauthorized",
  "forbidden_origin",
  "avatar_missing",
] as const;

export type ProfileErrorCode =
  | (typeof PROFILE_ERROR_CODES)[number]
  | "cancelled"
  | "invalid_response"
  | "response_too_large";

export class ProfileApiError extends Error {
  readonly field: string | null;
  readonly reason: string | null;

  constructor(
    readonly status: number,
    readonly code: ProfileErrorCode,
    details?: { field: string | null; reason: string | null },
  ) {
    super("The profile operation could not be completed.");
    this.name = "ProfileApiError";
    this.field = details?.field ?? null;
    this.reason = details?.reason ?? null;
  }
}

export interface ProfileIdentityV1 {
  name: string;
  purpose: string;
  avatar_asset_id: string | null;
  accent: "blue" | "green" | "orange" | "purple" | "red" | "slate";
  shape: "circle" | "rounded" | "square";
}

export interface ProfileRuntimeV1 {
  provider: string;
  model: string | null;
  effort: "low" | "medium" | "high" | "xhigh" | "max";
  permission_mode: "default" | "manual" | "acceptEdits" | "auto" | "plan" | null;
}

export interface ProfileLimitsV1 {
  timeout_seconds: number | null;
  max_root_tasks: number | null;
  token_budget: number | null;
  currency_budget: number | null;
}

export interface ProfileProvenanceV1 {
  kind: string;
  derived_from: Record<string, unknown> | null;
  native_source: Record<string, unknown> | null;
}

export interface ProfileDocumentV1 {
  format_version: 1;
  identity: ProfileIdentityV1;
  runtime: ProfileRuntimeV1;
  instructions: string;
  workspace: { allowed_workspace_ids: string[] };
  context: { items: Record<string, unknown>[] };
  native_assets: Record<string, unknown>[];
  tools: {
    required: string[];
    denied: string[];
    mcp_configuration: Record<string, unknown> | null;
  };
  memory: {
    mode: string;
    sources: Record<string, unknown>[];
  };
  limits: ProfileLimitsV1;
  provenance: ProfileProvenanceV1;
}

export function blankProfileDocument(): ProfileDocumentV1 {
  return {
    format_version: 1,
    identity: {
      name: "New profile",
      purpose: "",
      avatar_asset_id: null,
      accent: "blue",
      shape: "rounded",
    },
    runtime: { provider: "claude", model: null, effort: "high", permission_mode: null },
    instructions: "",
    workspace: { allowed_workspace_ids: [] },
    context: { items: [] },
    native_assets: [],
    tools: { required: [], denied: [], mcp_configuration: null },
    memory: { mode: "provider_inherited", sources: [] },
    limits: {
      timeout_seconds: null,
      max_root_tasks: null,
      token_budget: null,
      currency_budget: null,
    },
    provenance: { kind: "user", derived_from: null, native_source: null },
  };
}

export interface ProfileSummary {
  profile_id: string;
  revision: number;
  current_revision: number;
  state_revision: number;
  created_at: string;
  updated_at: string;
  archived: boolean;
  identity: ProfileIdentityV1;
  runtime: ProfileRuntimeV1;
  provenance: ProfileProvenanceV1;
}

export interface ProfileDefinition {
  profile_id: string;
  revision: number;
  current_revision: number;
  state_revision: number;
  created_at: string;
  updated_at: string;
  archived: boolean;
  document: ProfileDocumentV1;
}

export interface ProfileReceipt {
  profile_id: string;
  revision: number;
  state_revision: number;
}

export interface ProfileChange {
  field: string;
  before: unknown;
  after: unknown;
}

export interface ProfileComparison {
  profile_id: string;
  left: number;
  right: number;
  changes: ProfileChange[];
}

export interface ProfileApi {
  importMcpConfiguration?(json: string, signal?: AbortSignal): Promise<Record<string, unknown>>;
  list(archived?: boolean, signal?: AbortSignal): Promise<ProfileSummary[]>;
  get(profileId: string, revision?: number, signal?: AbortSignal): Promise<ProfileDefinition>;
  create(
    profileId: string,
    commandId: string,
    document: ProfileDocumentV1,
    signal?: AbortSignal,
  ): Promise<ProfileReceipt>;
  revise(
    profileId: string,
    commandId: string,
    expectedRevision: number,
    document: ProfileDocumentV1,
    signal?: AbortSignal,
  ): Promise<ProfileReceipt>;
  archive(
    profileId: string,
    commandId: string,
    expectedStateRevision: number,
    archived: boolean,
    signal?: AbortSignal,
  ): Promise<ProfileReceipt>;
  duplicate(
    profileId: string,
    commandId: string,
    expectedRevision: number,
    targetProfileId: string,
    targetRuntime: string,
    signal?: AbortSignal,
  ): Promise<ProfileReceipt>;
  compare(
    profileId: string,
    left: number,
    right: number,
    signal?: AbortSignal,
  ): Promise<ProfileComparison>;
}

const RESPONSE_MAX_BYTES = 2 << 20;
const TIMEOUT_MS = 15_000;
const LIST_MAX = 64;
const NAME_MAX_BYTES = 128;
const PURPOSE_MAX_BYTES = 2048;
const PROVIDER_MAX_BYTES = 4096;
const MODEL_MAX_BYTES = 4096;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const TEXT = new TextEncoder();

export const ACCENTS: readonly string[] = ["blue", "green", "orange", "purple", "red", "slate"];
const SHAPES: readonly string[] = ["circle", "rounded", "square"];
const EFFORTS: readonly string[] = ["low", "medium", "high", "xhigh", "max"];
const PERMISSION_MODES: readonly string[] = ["default", "manual", "acceptEdits", "auto", "plan"];

const SUMMARY_KEYS = [
  "profile_id",
  "revision",
  "current_revision",
  "state_revision",
  "created_at",
  "updated_at",
  "archived",
  "identity",
  "runtime",
  "provenance",
] as const;

const DEFINITION_KEYS = [
  "profile_id",
  "revision",
  "current_revision",
  "state_revision",
  "created_at",
  "updated_at",
  "archived",
  "document",
] as const;

const DOCUMENT_KEYS = [
  "format_version",
  "identity",
  "runtime",
  "instructions",
  "workspace",
  "context",
  "native_assets",
  "tools",
  "memory",
  "limits",
  "provenance",
] as const;

const IDENTITY_KEYS = ["name", "purpose", "avatar_asset_id", "accent", "shape"] as const;
const RUNTIME_KEYS = ["provider", "model", "effort", "permission_mode"] as const;
const PROVENANCE_KEYS = ["kind", "derived_from", "native_source"] as const;
const LIMITS_KEYS = [
  "timeout_seconds",
  "max_root_tasks",
  "token_budget",
  "currency_budget",
] as const;
const WORKSPACE_KEYS = ["allowed_workspace_ids"] as const;
const CONTEXT_KEYS = ["items"] as const;
const TOOLS_KEYS = ["required", "denied", "mcp_configuration"] as const;
const MEMORY_KEYS = ["mode", "sources"] as const;
const RECEIPT_KEYS = ["profile_id", "revision", "state_revision"] as const;
const COMPARISON_KEYS = ["profile_id", "left", "right", "changes"] as const;
const CHANGE_KEYS = ["field", "before", "after"] as const;
const LIST_KEYS = ["profiles"] as const;

const ALLOWED_ERROR_CODES = new Set<string>(PROFILE_ERROR_CODES);

function badInput(): never {
  throw new ProfileApiError(0, "invalid_input");
}

function badResponse(): never {
  throw new ProfileApiError(0, "invalid_response");
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactKeys(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  if (!isPlainObject(value) || Object.keys(value).length !== keys.length) return false;
  return keys.every((key) => key in value);
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

function isRevision(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
}

function isTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || !TIMESTAMP.test(value)) return false;
  const time = Date.parse(value);
  return Number.isFinite(time) && new Date(time).toISOString() === value;
}

function inByteRange(value: unknown, minimum: number, maximum: number): value is string {
  if (typeof value !== "string" || !value.isWellFormed()) return false;
  const size = TEXT.encode(value).byteLength;
  return size >= minimum && size <= maximum;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isObjectArray(value: unknown): value is Record<string, unknown>[] {
  return Array.isArray(value) && value.every(isPlainObject);
}

function isNullableObject(value: unknown): value is Record<string, unknown> | null {
  return value === null || isPlainObject(value);
}

function isSafeJson(value: unknown, depth = 0): boolean {
  if (depth > 64) return false;
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "string") return value.isWellFormed();
  if (typeof value === "number") return Number.isSafeInteger(value);
  if (Array.isArray(value)) return value.every((item) => isSafeJson(item, depth + 1));
  if (!isPlainObject(value)) return false;
  return Object.entries(value).every(
    ([key, item]) =>
      !["path", "argv", "environment", "executable", "secret"].includes(key) &&
      isSafeJson(item, depth + 1),
  );
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || isRevision(value);
}

function validateIdentity(value: unknown): ProfileIdentityV1 | null {
  if (!exactKeys(value, IDENTITY_KEYS)) return null;
  if (!inByteRange(value.name, 1, NAME_MAX_BYTES)) return null;
  if (!inByteRange(value.purpose, 0, PURPOSE_MAX_BYTES)) return null;
  const avatar = value.avatar_asset_id;
  if (!(avatar === null || isUuid(avatar))) return null;
  if (typeof value.accent !== "string" || !ACCENTS.includes(value.accent)) return null;
  if (typeof value.shape !== "string" || !SHAPES.includes(value.shape)) return null;
  return value as unknown as ProfileIdentityV1;
}

function validateRuntime(value: unknown): ProfileRuntimeV1 | null {
  if (!exactKeys(value, RUNTIME_KEYS)) return null;
  if (!inByteRange(value.provider, 1, PROVIDER_MAX_BYTES)) return null;
  const model = value.model;
  if (!(model === null || inByteRange(model, 1, MODEL_MAX_BYTES))) return null;
  if (typeof value.effort !== "string" || !EFFORTS.includes(value.effort)) return null;
  const permission = value.permission_mode;
  const validPermission =
    permission === null ||
    (typeof permission === "string" && PERMISSION_MODES.includes(permission));
  if (!validPermission) return null;
  return value as unknown as ProfileRuntimeV1;
}

function validateProvenance(value: unknown): ProfileProvenanceV1 | null {
  if (!exactKeys(value, PROVENANCE_KEYS)) return null;
  const kind = value.kind;
  if (!inByteRange(kind, 1, PROVIDER_MAX_BYTES)) return null;
  if (!isNullableObject(value.derived_from)) return null;
  if (!isNullableObject(value.native_source)) return null;
  return value as unknown as ProfileProvenanceV1;
}

function validateLimits(value: unknown): ProfileLimitsV1 | null {
  if (!exactKeys(value, LIMITS_KEYS)) return null;
  for (const key of LIMITS_KEYS) {
    if (!isNullableNumber(value[key])) return null;
  }
  return value as unknown as ProfileLimitsV1;
}

function validateTools(value: unknown): ProfileDocumentV1["tools"] | null {
  if (!exactKeys(value, TOOLS_KEYS)) return null;
  if (!isStringArray(value.required)) return null;
  if (!isStringArray(value.denied)) return null;
  for (const names of [value.required, value.denied]) {
    if (
      names.length > 64 ||
      !names.every((name) => inByteRange(name, 1, PROVIDER_MAX_BYTES)) ||
      new Set(names).size !== names.length
    )
      return null;
  }
  if (!isNullableObject(value.mcp_configuration)) return null;
  return value as unknown as ProfileDocumentV1["tools"];
}

function importedServer(value: unknown): boolean {
  if (!exactKeys(value, ["name", "transport", "command", "args", "url", "note"])) return false;
  if (!inByteRange(value.name, 1, 26) || !isStringArray(value.args) || value.args.length > 32)
    return false;
  if (!value.args.every((arg) => inByteRange(arg, 0, 1024))) return false;
  if (value.note !== null) return false;
  if (value.transport === "http")
    return value.command === null && value.args.length === 0 && inByteRange(value.url, 1, 2048);
  return value.transport === "stdio" && inByteRange(value.command, 1, 1024) && value.url === null;
}

function validateMemory(value: unknown): ProfileDocumentV1["memory"] | null {
  if (!exactKeys(value, MEMORY_KEYS)) return null;
  const mode = value.mode;
  if (!inByteRange(mode, 1, PROVIDER_MAX_BYTES)) return null;
  if (!isObjectArray(value.sources)) return null;
  return value as unknown as ProfileDocumentV1["memory"];
}

function validateDocument(value: unknown): ProfileDocumentV1 | null {
  if (!exactKeys(value, DOCUMENT_KEYS) || !isSafeJson(value)) return null;
  if (value.format_version !== 1) return null;
  if (validateIdentity(value.identity) === null) return null;
  if (validateRuntime(value.runtime) === null) return null;
  if (!inByteRange(value.instructions, 0, 65_536)) return null;
  const workspace = value.workspace;
  if (!exactKeys(workspace, WORKSPACE_KEYS)) return null;
  if (
    !isStringArray(workspace.allowed_workspace_ids) ||
    workspace.allowed_workspace_ids.length > 32 ||
    !workspace.allowed_workspace_ids.every(isUuid)
  )
    return null;
  const context = value.context;
  if (!exactKeys(context, CONTEXT_KEYS)) return null;
  if (!isObjectArray(context.items) || context.items.length > 32) return null;
  if (!isObjectArray(value.native_assets) || value.native_assets.length > 32) return null;
  if (validateTools(value.tools) === null) return null;
  if (validateMemory(value.memory) === null) return null;
  if (validateLimits(value.limits) === null) return null;
  if (validateProvenance(value.provenance) === null) return null;
  try {
    const encoded = JSON.stringify(value);
    if (!encoded.isWellFormed() || TEXT.encode(encoded).byteLength > 262_144) return null;
  } catch {
    return null;
  }
  return value as unknown as ProfileDocumentV1;
}

function validateSummary(value: unknown): ProfileSummary | null {
  if (!exactKeys(value, SUMMARY_KEYS)) return null;
  if (!isUuid(value.profile_id)) return null;
  if (!isRevision(value.revision)) return null;
  if (!isRevision(value.current_revision)) return null;
  if (value.revision !== value.current_revision) return null;
  if (!isRevision(value.state_revision)) return null;
  if (!isTimestamp(value.created_at)) return null;
  if (!isTimestamp(value.updated_at)) return null;
  if (typeof value.archived !== "boolean") return null;
  if (validateIdentity(value.identity) === null) return null;
  if (validateRuntime(value.runtime) === null) return null;
  if (validateProvenance(value.provenance) === null) return null;
  return value as unknown as ProfileSummary;
}

function validateList(value: unknown): ProfileSummary[] | null {
  if (!exactKeys(value, LIST_KEYS)) return null;
  const rows = value.profiles;
  if (!Array.isArray(rows) || rows.length > LIST_MAX) return null;
  const summaries: ProfileSummary[] = [];
  for (const row of rows) {
    const summary = validateSummary(row);
    if (summary === null) return null;
    summaries.push(summary);
  }
  if (new Set(summaries.map((item) => item.profile_id)).size !== summaries.length) return null;
  return summaries;
}

function validateDefinition(value: unknown): ProfileDefinition | null {
  if (!exactKeys(value, DEFINITION_KEYS)) return null;
  if (
    !isUuid(value.profile_id) ||
    !isRevision(value.revision) ||
    !isRevision(value.current_revision) ||
    !isRevision(value.state_revision) ||
    !isTimestamp(value.created_at) ||
    !isTimestamp(value.updated_at) ||
    typeof value.archived !== "boolean"
  )
    return null;
  const document = validateDocument(value.document);
  if (document === null) return null;
  return { ...(value as Omit<ProfileDefinition, "document">), document };
}

function validateReceipt(
  value: unknown,
  profileId: string,
  revision: number | null,
  stateRevision: number | null,
): ProfileReceipt | null {
  if (!exactKeys(value, RECEIPT_KEYS)) return null;
  if (value.profile_id !== profileId) return null;
  const actualRevision = value.revision;
  if (!isRevision(actualRevision) || (revision !== null && actualRevision !== revision))
    return null;
  const actualStateRevision = value.state_revision;
  if (
    !isRevision(actualStateRevision) ||
    (stateRevision !== null && actualStateRevision !== stateRevision)
  )
    return null;
  return { profile_id: profileId, revision: actualRevision, state_revision: actualStateRevision };
}

function validateChange(value: unknown): ProfileChange | null {
  if (!exactKeys(value, CHANGE_KEYS)) return null;
  if (!inByteRange(value.field, 1, PROVIDER_MAX_BYTES)) return null;
  if (!isSafeJson(value.before) || !isSafeJson(value.after)) return null;
  return value as unknown as ProfileChange;
}

function validateComparison(
  value: unknown,
  profileId: string,
  left: number,
  right: number,
): ProfileComparison | null {
  if (!exactKeys(value, COMPARISON_KEYS)) return null;
  if (value.profile_id !== profileId) return null;
  if (value.left !== left || value.right !== right) return null;
  const rows = value.changes;
  if (!Array.isArray(rows)) return null;
  const changes: ProfileChange[] = [];
  for (const row of rows) {
    const change = validateChange(row);
    if (change === null) return null;
    changes.push(change);
  }
  if (new Set(changes.map((change) => change.field)).size !== changes.length) return null;
  return { profile_id: profileId, left, right, changes };
}

function serverError(status: number, body: unknown): ProfileApiError {
  let code: ProfileErrorCode =
    status === 400
      ? "invalid_input"
      : status === 401
        ? "unauthorized"
        : status === 403
          ? "forbidden_origin"
          : status === 404
            ? "not_found"
            : status === 408
              ? "intake_timeout"
              : status === 409
                ? "conflict"
                : status === 413
                  ? "too_large"
                  : status === 429
                    ? "overloaded"
                    : "unavailable";
  if (isPlainObject(body)) {
    const error = body.error;
    if (isPlainObject(error)) {
      const candidate = error.code;
      if (typeof candidate === "string" && ALLOWED_ERROR_CODES.has(candidate)) {
        code = candidate as ProfileErrorCode;
      }
    }
  }
  let details: { field: string | null; reason: string | null } | undefined;
  if (status === 400 && code === "invalid_document" && isPlainObject(body)) {
    const error = body.error;
    if (isPlainObject(error)) {
      const field = error.field;
      const reason = error.reason;
      if (
        typeof field === "string" &&
        field.length <= 256 &&
        typeof reason === "string" &&
        reason.length <= 256
      )
        details = { field, reason };
    }
  }
  return new ProfileApiError(status, code, details);
}

export function createProfileApi(token: string, fetchImpl: typeof fetch = fetch): ProfileApi {
  async function request(
    path: string,
    method: "GET" | "POST" | "PATCH",
    body: string | undefined,
    signal: AbortSignal | undefined,
  ): Promise<unknown> {
    if (signal?.aborted) throw new ProfileApiError(0, "cancelled");
    const controller = new AbortController();
    const relay = (): void => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT_MS);
    try {
      const response = await fetchImpl(path, {
        method,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body !== undefined ? { "content-type": "application/json" } : {}),
        },
        signal: controller.signal,
        ...(body !== undefined ? { body } : {}),
      });
      let parsed: unknown;
      try {
        parsed = await boundedJson(response, RESPONSE_MAX_BYTES);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        if (error instanceof Error && error.message === "response_too_large")
          throw new ProfileApiError(0, "response_too_large");
        if (!response.ok) throw serverError(response.status, null);
        throw new ProfileApiError(0, "invalid_response");
      }
      if (!response.ok) throw serverError(response.status, parsed);
      return parsed;
    } catch (error) {
      if (error instanceof ProfileApiError) throw error;
      if (signal?.aborted) throw new ProfileApiError(0, "cancelled");
      if (timedOut) throw new ProfileApiError(408, "intake_timeout");
      throw new ProfileApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }

  return {
    async importMcpConfiguration(json, signal) {
      if (!inByteRange(json, 1, 64 << 10)) return badInput();
      const body = await request(
        "/api/v2/profiles/mcp/import",
        "POST",
        JSON.stringify({ json }),
        signal,
      );
      if (
        !exactKeys(body, ["servers"]) ||
        !Array.isArray(body.servers) ||
        body.servers.length < 1 ||
        body.servers.length > 8 ||
        !body.servers.every(importedServer) ||
        new Set(body.servers.map((server) => server.name)).size !== body.servers.length ||
        !isSafeJson(body)
      )
        return badResponse();
      return body;
    },

    async list(archived, signal) {
      const path = `/api/v2/profiles?archived=${archived ? "true" : "false"}`;
      const body = await request(path, "GET", undefined, signal);
      return validateList(body) ?? badResponse();
    },

    async get(profileId, revision, signal) {
      if (!isUuid(profileId) || (revision !== undefined && !isRevision(revision))) {
        return badInput();
      }
      const suffix = revision === undefined ? "" : `?revision=${revision}`;
      const body = await request(
        `/api/v2/profiles/${profileId}${suffix}`,
        "GET",
        undefined,
        signal,
      );
      const definition = validateDefinition(body) ?? badResponse();
      if (
        definition.profile_id !== profileId ||
        (revision === undefined
          ? definition.revision !== definition.current_revision
          : definition.revision !== revision)
      )
        return badResponse();
      return definition;
    },

    async create(profileId, commandId, document, signal) {
      if (!isUuid(profileId) || !isUuid(commandId) || validateDocument(document) === null)
        return badInput();
      const body = await request(
        "/api/v2/profiles",
        "POST",
        JSON.stringify({ profile_id: profileId, command_id: commandId, document }),
        signal,
      );
      return validateReceipt(body, profileId, 1, 1) ?? badResponse();
    },

    async revise(profileId, commandId, expectedRevision, document, signal) {
      if (
        !isUuid(profileId) ||
        !isUuid(commandId) ||
        !isRevision(expectedRevision) ||
        expectedRevision === Number.MAX_SAFE_INTEGER ||
        validateDocument(document) === null
      ) {
        return badInput();
      }
      const body = await request(
        `/api/v2/profiles/${profileId}`,
        "PATCH",
        JSON.stringify({
          command_id: commandId,
          expected_revision: expectedRevision,
          document,
        }),
        signal,
      );
      return validateReceipt(body, profileId, expectedRevision + 1, null) ?? badResponse();
    },

    async archive(profileId, commandId, expectedStateRevision, archived, signal) {
      if (
        !isUuid(profileId) ||
        !isUuid(commandId) ||
        !isRevision(expectedStateRevision) ||
        expectedStateRevision === Number.MAX_SAFE_INTEGER ||
        typeof archived !== "boolean"
      ) {
        return badInput();
      }
      const body = await request(
        `/api/v2/profiles/${profileId}/archive`,
        "POST",
        JSON.stringify({
          command_id: commandId,
          expected_state_revision: expectedStateRevision,
          archived,
        }),
        signal,
      );
      return validateReceipt(body, profileId, null, expectedStateRevision + 1) ?? badResponse();
    },

    async duplicate(
      profileId,
      commandId,
      expectedRevision,
      targetProfileId,
      targetRuntime,
      signal,
    ) {
      if (
        !isUuid(profileId) ||
        !isUuid(targetProfileId) ||
        !isUuid(commandId) ||
        !isRevision(expectedRevision) ||
        !inByteRange(targetRuntime, 1, PROVIDER_MAX_BYTES)
      ) {
        return badInput();
      }
      const body = await request(
        `/api/v2/profiles/${profileId}/duplicate`,
        "POST",
        JSON.stringify({
          command_id: commandId,
          expected_revision: expectedRevision,
          target_profile_id: targetProfileId,
          target_runtime: targetRuntime,
        }),
        signal,
      );
      return validateReceipt(body, targetProfileId, 1, 1) ?? badResponse();
    },

    async compare(profileId, left, right, signal) {
      if (!isUuid(profileId) || !isRevision(left) || !isRevision(right)) return badInput();
      const path = `/api/v2/profiles/${profileId}/compare?left=${left}&right=${right}`;
      const body = await request(path, "GET", undefined, signal);
      return validateComparison(body, profileId, left, right) ?? badResponse();
    },
  };
}
