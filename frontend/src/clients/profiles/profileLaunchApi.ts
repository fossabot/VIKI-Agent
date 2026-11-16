/** Bounded client for provider evidence and profile draft preparation. */

import type { Accent, Shape } from "../../components/Avatar/identity";
import { boundedJson } from "../boundedResponse";
import { ACCENTS } from "./profileApi";

export type EvidenceStatus = "supported" | "unsupported" | "unverified";
export interface CapabilityEvidence {
  id: string;
  status: EvidenceStatus;
  reason: string | null;
  evidence: string | null;
}
export interface ProviderCatalogView {
  provider: string;
  adapter_version: string;
  native_version: string | null;
  observed_at: string | null;
  catalog_key: string | null;
  capabilities: CapabilityEvidence[];
}
export interface ProviderModelView extends Omit<ProviderCatalogView, "capabilities"> {
  status: "supported" | "unverified";
  models: { value: string; label: string; description: string }[];
}
export interface ProfileFieldPreview {
  field: string;
  preview_class: string;
  status: EvidenceStatus;
  effective_value: unknown;
  source: string;
  reason: string | null;
}
export interface BlockedProfilePreview {
  launchable: false;
  fields: ProfileFieldPreview[];
  blockers: string[];
  profile_id: string;
  profile_revision: number;
}
export interface ProfileSessionIdentity {
  name: string;
  avatar_asset_id: string | null;
  accent: Accent;
  shape: Shape;
  profile_revision: number;
}
export interface PreparedProfilePreview {
  launchable: boolean;
  fields: ProfileFieldPreview[];
  blockers: string[];
  warnings: string[];
  prefs: { mode: string; effort: string; model: string | null };
  session_id: string;
  profile_id: string;
  profile_revision: number;
  generation: string | null;
  workspace_id: string;
  workspace_revision: number;
  policy: string;
  policy_revision: number;
  settings_revision: number;
  runtime: string;
  capability_key: string;
  draft: {
    key: string;
    revision: number;
    text: string;
    saved_at: string | null;
    attachments: Record<string, unknown>[];
  };
  identity?: ProfileSessionIdentity;
  // Only a never-started session reopens without a transcript.
  started?: boolean;
  mcp_config?: { json: string; argv_element: string } | null;
  bot_memory?: BotMemory;
  // Present only for a positive Bot preview; a null branch means the copy is made on first send.
  code_copy?: { workspace_id: string; name: string; branch: string | null } | null;
}
export type ProfilePreview = BlockedProfilePreview | PreparedProfilePreview;

export type ProfileLaunchErrorCode =
  | "cancelled"
  | "invalid_input"
  | "invalid_response"
  | "response_too_large"
  | "not_found"
  | "conflict"
  | "command_conflict"
  | "capacity"
  | "newer_version"
  | "workspace_restart_required"
  | "session_exists"
  | "transcript_missing"
  | "provider_unavailable"
  | "settings_unavailable"
  | "workspace_unavailable"
  | "capability_unverified"
  | "invalid_catalog"
  | "unauthorized"
  | "forbidden_origin"
  | "overloaded"
  | "intake_timeout"
  | "disconnected"
  | "unavailable";

export class ProfileLaunchApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: ProfileLaunchErrorCode,
  ) {
    super("The profile launch review could not be completed.");
    this.name = "ProfileLaunchApiError";
  }
}

export interface ProfileLaunchApi {
  capabilities(provider: string, signal?: AbortSignal): Promise<ProviderCatalogView>;
  models(provider: string, signal?: AbortSignal): Promise<ProviderModelView>;
  inspect(sessionId: string, signal?: AbortSignal): Promise<PreparedProfilePreview>;
  prepare(
    profileId: string,
    commandId: string,
    sessionId: string,
    expectedRevision: number,
    workspaceId: string,
    signal?: AbortSignal,
    botId?: string,
  ): Promise<ProfilePreview>;
}

// The server accepts any canonical lower-case UUID (`str(uuid.UUID(v)) == v`), not only
// versions 1-5: it never checks the version or variant nibbles.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const HEX = /^[0-9a-f]{64}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const ERRORS = new Set<ProfileLaunchErrorCode>([
  "invalid_input",
  "not_found",
  "conflict",
  "command_conflict",
  "capacity",
  "newer_version",
  "workspace_restart_required",
  "session_exists",
  "transcript_missing",
  "provider_unavailable",
  "settings_unavailable",
  "workspace_unavailable",
  "capability_unverified",
  "invalid_catalog",
  "unauthorized",
  "forbidden_origin",
  "overloaded",
  "intake_timeout",
  "disconnected",
  "unavailable",
]);
const MAX_RESPONSE = 2 << 20;
// Matches the server's profile_document.MAX_STRING_BYTES, which a connection ref can approach.
const MAX_PREFS_MODEL = 4096;
const TIMEOUT = 15_000;
const SHAPES: readonly string[] = ["circle", "rounded", "square"];

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
function uuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}
function revision(value: unknown, zero = false): value is number {
  return Number.isSafeInteger(value) && (value as number) >= (zero ? 0 : 1);
}
function nullableText(value: unknown, max = 512): value is string | null {
  return value === null || text(value, max);
}
function codes(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.length <= 64 &&
    value.every((item) => text(item, 128) && CODE.test(item)) &&
    new Set(value).size === value.length
  );
}
function jsonValue(value: unknown, depth = 0): boolean {
  if (
    value === null ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isSafeInteger(value))
  )
    return true;
  if (typeof value === "string") return text(value, 131_072, 0);
  if (depth >= 8) return false;
  if (Array.isArray(value))
    return value.length <= 256 && value.every((item) => jsonValue(item, depth + 1));
  return (
    object(value) &&
    Object.keys(value).length <= 256 &&
    Object.entries(value).every(([key, item]) => text(key, 512) && jsonValue(item, depth + 1))
  );
}

const COMMON = ["provider", "adapter_version", "native_version", "observed_at", "catalog_key"];
function common(value: Record<string, unknown>): boolean {
  return (
    text(value.provider, 32) &&
    text(value.adapter_version, 64) &&
    nullableText(value.native_version, 64) &&
    (value.observed_at === null ||
      (typeof value.observed_at === "string" && TIMESTAMP.test(value.observed_at))) &&
    (value.catalog_key === null ||
      (typeof value.catalog_key === "string" && HEX.test(value.catalog_key)))
  );
}
function capability(value: unknown): value is CapabilityEvidence {
  return (
    exact(value, ["id", "status", "reason", "evidence"]) &&
    text(value.id, 128) &&
    CODE.test(value.id) &&
    (value.status === "supported" ||
      value.status === "unsupported" ||
      value.status === "unverified") &&
    (value.reason === null || (text(value.reason, 128) && CODE.test(value.reason))) &&
    (value.evidence === null || (text(value.evidence, 128) && CODE.test(value.evidence)))
  );
}
function catalog(value: unknown): ProviderCatalogView | null {
  if (
    !exact(value, [...COMMON, "capabilities"]) ||
    !common(value) ||
    !Array.isArray(value.capabilities) ||
    value.capabilities.length > 64 ||
    !value.capabilities.every(capability) ||
    new Set(value.capabilities.map((item) => item.id)).size !== value.capabilities.length
  )
    return null;
  return value as unknown as ProviderCatalogView;
}
function modelView(value: unknown): ProviderModelView | null {
  if (
    !exact(value, [...COMMON, "status", "models"]) ||
    !common(value) ||
    (value.status !== "supported" && value.status !== "unverified") ||
    !Array.isArray(value.models) ||
    value.models.length > 128 ||
    !value.models.every(
      (row) =>
        exact(row, ["value", "label", "description"]) &&
        // 4,096 bytes matches the server's MAX_DETAIL_BYTES (src/viki_agent/profiles/provider_catalogs.py:108-109).
        text(row.value, 4096) &&
        text(row.label, 4096) &&
        text(row.description, 4096, 0),
    ) ||
    new Set(value.models.map((item) => item.value)).size !== value.models.length
  )
    return null;
  return value as unknown as ProviderModelView;
}
function field(value: unknown): value is ProfileFieldPreview {
  return (
    exact(value, ["field", "preview_class", "status", "effective_value", "source", "reason"]) &&
    text(value.field, 128) &&
    text(value.preview_class, 64) &&
    text(value.source, 64) &&
    (value.status === "supported" ||
      value.status === "unsupported" ||
      value.status === "unverified") &&
    (value.reason === null || text(value.reason, 128)) &&
    jsonValue(value.effective_value)
  );
}
function basePreview(value: Record<string, unknown>): boolean {
  return (
    typeof value.launchable === "boolean" &&
    Array.isArray(value.fields) &&
    value.fields.length <= 64 &&
    value.fields.every(field) &&
    new Set(value.fields.map((item) => item.field)).size === value.fields.length &&
    codes(value.blockers) &&
    value.launchable === (value.blockers.length === 0) &&
    uuid(value.profile_id) &&
    revision(value.profile_revision)
  );
}
function sessionIdentity(
  value: unknown,
  profileRevision: unknown,
): value is ProfileSessionIdentity {
  return (
    exact(value, ["name", "avatar_asset_id", "accent", "shape", "profile_revision"]) &&
    text(value.name, 128) &&
    (value.avatar_asset_id === null || uuid(value.avatar_asset_id)) &&
    typeof value.accent === "string" &&
    ACCENTS.includes(value.accent) &&
    typeof value.shape === "string" &&
    SHAPES.includes(value.shape) &&
    value.profile_revision === profileRevision
  );
}
function mcpConfig(value: unknown): boolean {
  return (
    value === null ||
    (exact(value, ["json", "argv_element"]) &&
      text(value.json, MAX_RESPONSE, 1) &&
      text(value.argv_element, 4096, 1) &&
      value.argv_element.startsWith("--mcp-config="))
  );
}
// A vault-kept Bot names a vault ref; an app-kept one names an absolute folder ending in its UUID.
// `routine` says the chat is one of the Bot's routine runs, which carry the same binding as its
// task threads.
export type BotMemory =
  | { bot_id: string; ref: string; routine: boolean }
  | { bot_id: string; path: string; routine: boolean };
export const botMemoryFolder = (memory: BotMemory): string =>
  "ref" in memory ? memory.ref : memory.path;
function botMemory(value: unknown): boolean {
  if (object(value) && "path" in value) {
    const path = value.path;
    return (
      exact(value, ["bot_id", "path", "routine"]) &&
      uuid(value.bot_id) &&
      typeof value.routine === "boolean" &&
      typeof path === "string" &&
      path.isWellFormed() &&
      path.length >= 1 &&
      path.length <= 4096 &&
      !/\p{Cc}/u.test(path) &&
      /^(\/|[A-Za-z]:[\\/])/.test(path) &&
      /[\\/]/.test(path.slice(-value.bot_id.length - 1, -value.bot_id.length)) &&
      path.endsWith(value.bot_id)
    );
  }
  return (
    exact(value, ["bot_id", "ref", "routine"]) &&
    uuid(value.bot_id) &&
    typeof value.routine === "boolean" &&
    value.ref === `bot-memory/${value.bot_id}`
  );
}
function codeCopy(value: unknown): boolean {
  return (
    value === null ||
    (exact(value, ["workspace_id", "name", "branch"]) &&
      uuid(value.workspace_id) &&
      text(value.name, 512) &&
      nullableText(value.branch, 512))
  );
}
function preview(
  value: unknown,
  status: number,
  nullableGeneration = false,
): ProfilePreview | null {
  const blockedKeys = ["launchable", "fields", "blockers", "profile_id", "profile_revision"];
  if (status === 422) {
    return exact(value, blockedKeys) && value.launchable === false && basePreview(value)
      ? (value as unknown as BlockedProfilePreview)
      : null;
  }
  const withIdentity = object(value) && "identity" in value;
  const withStarted = object(value) && "started" in value;
  const withMcp = object(value) && "mcp_config" in value;
  const withBotMemory = object(value) && "bot_memory" in value;
  const withCodeCopy = object(value) && "code_copy" in value;
  const keys = [
    ...blockedKeys,
    "warnings",
    "prefs",
    "session_id",
    "generation",
    "workspace_id",
    "workspace_revision",
    "policy",
    "policy_revision",
    "settings_revision",
    "runtime",
    "capability_key",
    "draft",
    ...(withIdentity ? ["identity"] : []),
    ...(withStarted ? ["started"] : []),
    ...(withMcp ? ["mcp_config"] : []),
    ...(withBotMemory ? ["bot_memory"] : []),
    ...(withCodeCopy ? ["code_copy"] : []),
  ];
  if (
    !exact(value, keys) ||
    !basePreview(value) ||
    (withIdentity && !sessionIdentity(value.identity, value.profile_revision)) ||
    (withStarted && typeof value.started !== "boolean") ||
    (withMcp && !mcpConfig(value.mcp_config)) ||
    (withBotMemory && !botMemory(value.bot_memory)) ||
    (withCodeCopy && !codeCopy(value.code_copy)) ||
    !codes(value.warnings) ||
    !uuid(value.session_id) ||
    !(uuid(value.generation) || (nullableGeneration && value.generation === null)) ||
    !uuid(value.workspace_id) ||
    !revision(value.workspace_revision) ||
    !text(value.policy, 64) ||
    !revision(value.policy_revision) ||
    !revision(value.settings_revision, true) ||
    !text(value.runtime, 32) ||
    typeof value.capability_key !== "string" ||
    !HEX.test(value.capability_key) ||
    !exact(value.prefs, ["mode", "effort", "model"]) ||
    !text(value.prefs.mode, 64) ||
    !text(value.prefs.effort, 16) ||
    !nullableText(value.prefs.model, MAX_PREFS_MODEL) ||
    !exact(value.draft, ["key", "revision", "text", "saved_at", "attachments"]) ||
    value.draft.key !== value.session_id ||
    !revision(value.draft.revision, true) ||
    !text(value.draft.text, MAX_RESPONSE, 0) ||
    !(
      value.draft.saved_at === null ||
      (typeof value.draft.saved_at === "string" && TIMESTAMP.test(value.draft.saved_at))
    ) ||
    !Array.isArray(value.draft.attachments) ||
    value.draft.attachments.length > 10 ||
    !value.draft.attachments.every((item) => object(item) && jsonValue(item))
  )
    return null;
  return value as unknown as PreparedProfilePreview;
}

export function parseProfilePreview(
  value: unknown,
  status: number,
  nullableGeneration = false,
): ProfilePreview | null {
  return preview(value, status, nullableGeneration);
}

function serverError(status: number, body: unknown): ProfileLaunchApiError {
  const candidate = object(body) && object(body.error) ? body.error.code : null;
  if (typeof candidate === "string" && ERRORS.has(candidate as ProfileLaunchErrorCode))
    return new ProfileLaunchApiError(status, candidate as ProfileLaunchErrorCode);
  return new ProfileLaunchApiError(
    status,
    status === 401 ? "unauthorized" : status === 403 ? "forbidden_origin" : "unavailable",
  );
}

export function createProfileLaunchApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): ProfileLaunchApi {
  async function request(
    path: string,
    method: "GET" | "POST",
    body?: string,
    signal?: AbortSignal,
  ): Promise<{ status: number; body: unknown }> {
    if (signal?.aborted) throw new ProfileLaunchApiError(0, "cancelled");
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
        method,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body ? { "content-type": "application/json" } : {}),
        },
        signal: controller.signal,
        ...(body ? { body } : {}),
      });
      let parsed: unknown;
      try {
        parsed = await boundedJson(response, MAX_RESPONSE);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        if (error instanceof Error && error.message === "response_too_large")
          throw new ProfileLaunchApiError(0, "response_too_large");
        if (!response.ok && response.status !== 422) throw serverError(response.status, null);
        throw new ProfileLaunchApiError(0, "invalid_response");
      }
      if (!response.ok && response.status !== 422) throw serverError(response.status, parsed);
      return { status: response.status, body: parsed };
    } catch (error) {
      if (error instanceof ProfileLaunchApiError) throw error;
      if (signal?.aborted) throw new ProfileLaunchApiError(0, "cancelled");
      if (timedOut) throw new ProfileLaunchApiError(408, "intake_timeout");
      throw new ProfileLaunchApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  function provider(value: string): void {
    if (!text(value, 32) || !/^[a-z][a-z0-9_-]*$/.test(value))
      throw new ProfileLaunchApiError(0, "invalid_input");
  }
  return {
    async capabilities(name, signal) {
      provider(name);
      const result = await request(
        `/api/v2/providers/${encodeURIComponent(name)}/capabilities`,
        "GET",
        undefined,
        signal,
      );
      const parsed = catalog(result.body);
      if (!parsed || parsed.provider !== name)
        throw new ProfileLaunchApiError(0, "invalid_response");
      return parsed;
    },
    async models(name, signal) {
      provider(name);
      const result = await request(
        `/api/v2/providers/${encodeURIComponent(name)}/models`,
        "GET",
        undefined,
        signal,
      );
      const parsed = modelView(result.body);
      if (!parsed || parsed.provider !== name)
        throw new ProfileLaunchApiError(0, "invalid_response");
      return parsed;
    },
    async inspect(sessionId, signal) {
      if (!uuid(sessionId)) throw new ProfileLaunchApiError(0, "invalid_input");
      const result = await request(
        `/api/v2/profile-sessions/${sessionId}`,
        "GET",
        undefined,
        signal,
      );
      const parsed = preview(result.body, result.status, true);
      if (!parsed || !("session_id" in parsed) || parsed.session_id !== sessionId)
        throw new ProfileLaunchApiError(0, "invalid_response");
      return parsed;
    },
    async prepare(profileId, commandId, sessionId, expectedRevision, workspaceId, signal, botId) {
      if (
        ![profileId, commandId, sessionId, workspaceId].every(uuid) ||
        !revision(expectedRevision) ||
        (botId !== undefined && !uuid(botId))
      )
        throw new ProfileLaunchApiError(0, "invalid_input");
      const result = await request(
        `/api/v2/profiles/${profileId}/start-preview`,
        "POST",
        JSON.stringify({
          command_id: commandId,
          session_id: sessionId,
          expected_profile_revision: expectedRevision,
          workspace_id: workspaceId,
          ...(botId !== undefined ? { bot_id: botId } : {}),
        }),
        signal,
      );
      const parsed = preview(result.body, result.status);
      if (
        !parsed ||
        parsed.profile_id !== profileId ||
        parsed.profile_revision !== expectedRevision ||
        ("session_id" in parsed && parsed.session_id !== sessionId) ||
        ("bot_memory" in parsed && parsed.bot_memory?.bot_id !== botId)
      )
        throw new ProfileLaunchApiError(0, "invalid_response");
      return parsed;
    },
  };
}
