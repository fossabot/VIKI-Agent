/** Bounded private Bots client. Conversation history stays with the native provider. */
import { boundedJson } from "../boundedResponse";
import { ACCENTS, type ProfileIdentityV1 } from "../profiles/profileApi";
import {
  type ProfileLaunchErrorCode,
  type ProfilePreview,
  parseProfilePreview,
} from "../profiles/profileLaunchApi";
import { type SavedBotThread, type SavedThreadState, savedBotThread } from "./botThreadGroups";

export type BotOutcome = "succeeded" | "failed" | "cancelled" | "expired" | "blocked" | "unknown";
export type BotAttention = "silent" | "defer" | "notify";
export type BotAttentionReason =
  | "muted_by_owner"
  | "later_by_owner"
  | "show_all"
  | "waiting_on_you"
  | "due_or_overdue"
  | "due_soon"
  | "runs_to_review"
  | "runs_working"
  | "inbox_pressure"
  | "inbox_below_nudge"
  | "uncommitted_changes"
  | "routines_paused"
  | "routine_soon"
  | "routine_later";
export type BotDeliveryState =
  | "held"
  | "off"
  | "unavailable"
  | "sending"
  | "sent"
  | "failed"
  | "unknown";
export type BotDeliveryReason =
  | "none"
  | "off"
  | "held"
  | "not_connected"
  | "workspace_mismatch"
  | "stale_bot"
  | "stale_consent"
  | "policy_changed"
  | "app_closing"
  | "send_failed"
  | "send_unknown"
  | "unsupported_outcome"
  | "capacity";
export interface BotDeliveryReceipt {
  attempt_id: string;
  routine_id: string;
  session_id: string;
  outcome: BotOutcome;
  attention: BotAttention;
  reason: BotAttentionReason;
  state: BotDeliveryState;
  delivery_reason: BotDeliveryReason;
  created_at: string;
  updated_at: string;
}
export interface BotDelivery {
  enabled: boolean;
  available: boolean;
  reason:
    | "off"
    | "ready"
    | "not_connected"
    | "workspace_mismatch"
    | "bot_unavailable"
    | "app_closing";
}
export interface BotCodeFolder {
  workspace_id: string;
  name: string;
}
export interface BotRow {
  bot_id: string;
  profile_id: string;
  workspace_id: string;
  chat_session_id: string;
  state_revision: number;
  created_at: string;
  updated_at: string;
  profile: { revision: number; archived: boolean; identity: ProfileIdentityV1 } | null;
  workspace: {
    name: string;
    policy: "ordinary" | "existing_readonly" | "protected";
    active: boolean;
  } | null;
  chat: { profile_revision: number | null; started: boolean; archived: boolean };
  routine_ids: string[];
  delivery: BotDelivery;
  latest_delivery: BotDeliveryReceipt | null;
  /** Absent from an older server's rows, null when no folder is granted. */
  code_folder?: BotCodeFolder | null;
}
export interface BotSchedulerStatus {
  state: "running" | "stopped" | "unavailable";
  attended: boolean;
  paused: boolean;
}
export interface BotsList {
  bots: BotRow[];
  scheduler: BotSchedulerStatus;
}
export interface BotChatStatus {
  state:
    | "unprepared"
    | "prepared"
    | "reopenable"
    | "archived"
    | "transcript_missing"
    | "blocked"
    | "unavailable";
  blockers: string[];
}
export interface BotDetail {
  bot: BotRow;
  chat_status: BotChatStatus;
}
export interface BotCreated {
  bot: BotRow;
  replayed: boolean;
}
export interface BotChanged {
  bot: BotRow;
}
export interface BotRemoved {
  removed: true;
  bot_id: string;
  paused_routine_ids: string[];
}
export interface BotsReset {
  workspace_id: string;
  removed_bot_ids: string[];
  paused_routine_ids: string[];
}
export interface BotChatResult {
  bot_id: string;
  state_revision: number;
  preview: ProfilePreview;
}
export interface BotThread {
  session_id: string;
  profile_revision: number;
  created_at: string;
  started: boolean;
  title: string;
  archived: boolean;
}
export interface BotThreads {
  bot_id: string;
  threads: BotThread[];
  has_more: boolean;
}
export type BotsErrorCode =
  | ProfileLaunchErrorCode
  | "busy"
  | "profile_missing"
  | "profile_archived"
  | "profile_changed"
  | "workspace_missing"
  | "delivery_unavailable"
  | "profile_blocked"
  | "corrupt"
  | "protection"
  | "app_closing"
  | "code_folder_needs_ordinary"
  | "code_folder_untrusted"
  | "code_folder_missing"
  | "code_folder_not_repository"
  | "code_folder_no_commits"
  | "code_folder_git_unavailable"
  | "git_settings_changed";
export class BotsApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: BotsErrorCode,
  ) {
    super("The Bot operation could not be completed.");
    this.name = "BotsApiError";
  }
}
export interface BotsApi {
  groups?(workspaceId: string, signal?: AbortSignal): Promise<{ threads: SavedBotThread[] }>;
  saveGroup?(
    workspaceId: string,
    threadId: string,
    expectedRevision: number,
    state: SavedThreadState,
  ): Promise<SavedBotThread>;
  removeGroup?(workspaceId: string, threadId: string, expectedRevision: number): Promise<void>;
  list(signal?: AbortSignal): Promise<BotsList>;
  get(botId: string, signal?: AbortSignal): Promise<BotDetail>;
  threads?(botId: string, signal?: AbortSignal): Promise<BotThreads>;
  create(
    botId: string,
    profileId: string,
    expectedProfileRevision: number,
    workspaceId: string,
    signal?: AbortSignal,
  ): Promise<BotCreated>;
  chat(bot: BotRow, signal?: AbortSignal): Promise<BotChatResult>;
  newChat(botId: string, expectedStateRevision: number, signal?: AbortSignal): Promise<BotChanged>;
  setDelivery(
    botId: string,
    expectedStateRevision: number,
    enabled: boolean,
    signal?: AbortSignal,
  ): Promise<BotChanged>;
  setCodeFolder?(
    botId: string,
    expectedStateRevision: number,
    workspaceId: string | null,
    signal?: AbortSignal,
  ): Promise<BotChanged>;
  remove(botId: string, expectedStateRevision: number, signal?: AbortSignal): Promise<BotRemoved>;
  reset(workspaceId: string, signal?: AbortSignal): Promise<BotsReset>;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const CODE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const ENCODER = new TextEncoder();
const RESPONSE_MAX = 256 << 10;
const LIST_MAX = 2 << 20;
const CHAT_MAX = 2 << 20;
const BODY_MAX = 4 << 10;
const TIMEOUT = 15_000;
const OUTCOMES: readonly BotOutcome[] = [
  "succeeded",
  "failed",
  "cancelled",
  "expired",
  "blocked",
  "unknown",
];
const ATTENTION: readonly BotAttention[] = ["silent", "defer", "notify"];
const REASONS: readonly BotAttentionReason[] = [
  "muted_by_owner",
  "later_by_owner",
  "show_all",
  "waiting_on_you",
  "due_or_overdue",
  "due_soon",
  "runs_to_review",
  "runs_working",
  "inbox_pressure",
  "inbox_below_nudge",
  "uncommitted_changes",
  "routines_paused",
  "routine_soon",
  "routine_later",
];
const DELIVERY_STATES: readonly BotDeliveryState[] = [
  "held",
  "off",
  "unavailable",
  "sending",
  "sent",
  "failed",
  "unknown",
];
const DELIVERY_REASONS: readonly BotDeliveryReason[] = [
  "none",
  "off",
  "held",
  "not_connected",
  "workspace_mismatch",
  "stale_bot",
  "stale_consent",
  "policy_changed",
  "app_closing",
  "send_failed",
  "send_unknown",
  "unsupported_outcome",
  "capacity",
];
const ERRORS = new Set<BotsErrorCode>([
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
  "busy",
  "profile_missing",
  "profile_archived",
  "profile_changed",
  "workspace_missing",
  "delivery_unavailable",
  "profile_blocked",
  "corrupt",
  "protection",
  "app_closing",
  "code_folder_needs_ordinary",
  "code_folder_untrusted",
  "code_folder_missing",
  "code_folder_not_repository",
  "code_folder_no_commits",
  "code_folder_git_unavailable",
  "git_settings_changed",
]);
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return (
    object(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value)
  );
}
function uuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}
function revision(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 1;
}
function text(value: unknown, maximum: number, minimum = 1): value is string {
  if (typeof value !== "string" || !value.isWellFormed()) return false;
  const bytes = ENCODER.encode(value).byteLength;
  return bytes >= minimum && bytes <= maximum;
}
function stamp(value: unknown): value is string {
  if (typeof value !== "string" || !STAMP.test(value)) return false;
  const time = Date.parse(value);
  return Number.isFinite(time) && new Date(time).toISOString() === value;
}
function member(value: unknown, choices: readonly string[]): boolean {
  return typeof value === "string" && choices.includes(value);
}
function ids(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.length <= 64 &&
    value.every(uuid) &&
    new Set(value).size === value.length
  );
}
function codes(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.length <= 64 &&
    value.every((item) => text(item, 128) && CODE.test(item)) &&
    new Set(value).size === value.length
  );
}
function identity(value: unknown): boolean {
  return (
    exact(value, ["name", "purpose", "avatar_asset_id", "accent", "shape"]) &&
    text(value.name, 128) &&
    text(value.purpose, 2048, 0) &&
    (value.avatar_asset_id === null || uuid(value.avatar_asset_id)) &&
    member(value.accent, ACCENTS) &&
    member(value.shape, ["circle", "rounded", "square"])
  );
}
function receipt(value: unknown): boolean {
  return (
    exact(value, [
      "attempt_id",
      "routine_id",
      "session_id",
      "outcome",
      "attention",
      "reason",
      "state",
      "delivery_reason",
      "created_at",
      "updated_at",
    ]) &&
    [value.attempt_id, value.routine_id, value.session_id].every(uuid) &&
    member(value.outcome, OUTCOMES) &&
    member(value.attention, ATTENTION) &&
    member(value.reason, REASONS) &&
    member(value.state, DELIVERY_STATES) &&
    member(value.delivery_reason, DELIVERY_REASONS) &&
    stamp(value.created_at) &&
    stamp(value.updated_at)
  );
}
function codeFolder(value: unknown): boolean {
  return (
    value === null ||
    (exact(value, ["workspace_id", "name"]) &&
      uuid(value.workspace_id) &&
      typeof value.name === "string" &&
      value.name.isWellFormed() &&
      value.name.length >= 1 &&
      value.name.length <= 256)
  );
}
function row(value: unknown): value is BotRow {
  if (
    !exact(value, [
      "bot_id",
      "profile_id",
      "workspace_id",
      "chat_session_id",
      "state_revision",
      "created_at",
      "updated_at",
      "profile",
      "workspace",
      "chat",
      "routine_ids",
      "delivery",
      "latest_delivery",
      // Additive: rows from before the grant carry no key at all.
      ...(object(value) && "code_folder" in value ? ["code_folder"] : []),
    ])
  )
    return false;
  if ("code_folder" in value && !codeFolder(value.code_folder)) return false;
  if (
    ![value.bot_id, value.profile_id, value.workspace_id, value.chat_session_id].every(uuid) ||
    !revision(value.state_revision) ||
    !stamp(value.created_at) ||
    !stamp(value.updated_at)
  )
    return false;
  if (
    value.profile !== null &&
    !(
      exact(value.profile, ["revision", "archived", "identity"]) &&
      revision(value.profile.revision) &&
      typeof value.profile.archived === "boolean" &&
      identity(value.profile.identity)
    )
  )
    return false;
  if (
    value.workspace !== null &&
    !(
      exact(value.workspace, ["name", "policy", "active"]) &&
      typeof value.workspace.name === "string" &&
      value.workspace.name.isWellFormed() &&
      value.workspace.name.length >= 1 &&
      value.workspace.name.length <= 256 &&
      member(value.workspace.policy, ["ordinary", "existing_readonly", "protected"]) &&
      typeof value.workspace.active === "boolean"
    )
  )
    return false;
  return (
    exact(value.chat, ["profile_revision", "started", "archived"]) &&
    (value.chat.profile_revision === null || revision(value.chat.profile_revision)) &&
    typeof value.chat.started === "boolean" &&
    typeof value.chat.archived === "boolean" &&
    ids(value.routine_ids) &&
    exact(value.delivery, ["enabled", "available", "reason"]) &&
    typeof value.delivery.enabled === "boolean" &&
    typeof value.delivery.available === "boolean" &&
    member(value.delivery.reason, [
      "off",
      "ready",
      "not_connected",
      "workspace_mismatch",
      "bot_unavailable",
      "app_closing",
    ]) &&
    (value.latest_delivery === null || receipt(value.latest_delivery))
  );
}
function invalid(code: "invalid_input" | "invalid_response"): never {
  throw new BotsApiError(0, code);
}
function serverError(status: number, body: unknown): BotsApiError {
  const code = object(body) && object(body.error) ? body.error.code : null;
  return new BotsApiError(
    status,
    typeof code === "string" && ERRORS.has(code as BotsErrorCode)
      ? (code as BotsErrorCode)
      : "unavailable",
  );
}

export function createBotsApi(token: string, fetchImpl: typeof fetch = fetch): BotsApi {
  const base = "/api/v2/bots";
  async function request(
    path: string,
    method: "GET" | "POST" | "PUT" | "DELETE",
    payload?: object,
    signal?: AbortSignal,
    chat = false,
    maximumBody = BODY_MAX,
  ): Promise<{ status: number; body: unknown }> {
    if (signal?.aborted) throw new BotsApiError(0, "cancelled");
    const body = payload === undefined ? undefined : JSON.stringify(payload);
    if (body !== undefined && ENCODER.encode(body).byteLength > maximumBody)
      return invalid("invalid_input");
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
        signal: controller.signal,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body !== undefined ? { "content-type": "application/json" } : {}),
        },
        ...(body !== undefined ? { body } : {}),
      });
      let parsed: unknown;
      try {
        const maximum = method === "GET" && path === base ? LIST_MAX : RESPONSE_MAX;
        parsed = await boundedJson(response, chat ? CHAT_MAX : maximum);
      } catch (error) {
        if (controller.signal.aborted) throw error;
        if (error instanceof Error && error.message === "response_too_large")
          throw new BotsApiError(0, "response_too_large");
        if (!response.ok) throw serverError(response.status, null);
        return invalid("invalid_response");
      }
      if (
        !response.ok &&
        !(chat && response.status === 422 && exact(parsed, ["bot_id", "state_revision", "preview"]))
      )
        throw serverError(response.status, parsed);
      return { status: response.status, body: parsed };
    } catch (error) {
      if (error instanceof BotsApiError) throw error;
      if (signal?.aborted) throw new BotsApiError(0, "cancelled");
      if (timedOut) throw new BotsApiError(408, "intake_timeout");
      throw new BotsApiError(0, "unavailable");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }
  function mutation(botId: string, expectedStateRevision: number): void {
    if (!uuid(botId) || !revision(expectedStateRevision)) invalid("invalid_input");
  }
  function changed(body: unknown, status: number, botId: string): BotChanged {
    if (status !== 200 || !exact(body, ["bot"]) || !row(body.bot) || body.bot.bot_id !== botId)
      return invalid("invalid_response");
    return { bot: body.bot };
  }
  return {
    async groups(workspaceId, signal) {
      if (!uuid(workspaceId)) return invalid("invalid_input");
      const { body } = await request(
        `/api/v2/bot-threads/${workspaceId}`,
        "GET",
        undefined,
        signal,
        true,
      );
      if (
        !exact(body, ["threads"]) ||
        !Array.isArray(body.threads) ||
        body.threads.length > 20 ||
        !body.threads.every((row) => savedBotThread(row) && row.workspace_id === workspaceId) ||
        new Set(body.threads.map((row) => row.thread_id)).size !== body.threads.length
      )
        return invalid("invalid_response");
      return body as unknown as { threads: SavedBotThread[] };
    },
    async saveGroup(workspaceId, threadId, expectedRevision, state) {
      if (
        !uuid(workspaceId) ||
        !uuid(threadId) ||
        !Number.isSafeInteger(expectedRevision) ||
        expectedRevision < 0
      )
        return invalid("invalid_input");
      const { body } = await request(
        `/api/v2/bot-threads/${workspaceId}/${threadId}`,
        "PUT",
        { expected_revision: expectedRevision, state },
        undefined,
        false,
        65536,
      );
      if (
        !savedBotThread(body) ||
        body.thread_id !== threadId ||
        body.workspace_id !== workspaceId ||
        body.revision !== expectedRevision + 1
      )
        return invalid("invalid_response");
      return body;
    },
    async removeGroup(workspaceId, threadId, expectedRevision) {
      if (!uuid(workspaceId) || !uuid(threadId) || !revision(expectedRevision))
        return invalid("invalid_input");
      const { body } = await request(`/api/v2/bot-threads/${workspaceId}/${threadId}`, "DELETE", {
        expected_revision: expectedRevision,
      });
      if (!exact(body, ["removed"]) || body.removed !== true) return invalid("invalid_response");
    },
    async list(signal) {
      const { body, status } = await request(base, "GET", undefined, signal);
      if (
        status !== 200 ||
        !exact(body, ["bots", "scheduler"]) ||
        !Array.isArray(body.bots) ||
        body.bots.length > 64 ||
        !body.bots.every(row) ||
        new Set(body.bots.map((item) => item.bot_id)).size !== body.bots.length ||
        !exact(body.scheduler, ["state", "attended", "paused"]) ||
        !member(body.scheduler.state, ["running", "stopped", "unavailable"]) ||
        typeof body.scheduler.attended !== "boolean" ||
        typeof body.scheduler.paused !== "boolean"
      )
        return invalid("invalid_response");
      return body as unknown as BotsList;
    },
    async get(botId, signal) {
      if (!uuid(botId)) return invalid("invalid_input");
      const { body, status } = await request(`${base}/${botId}`, "GET", undefined, signal);
      if (
        status !== 200 ||
        !exact(body, ["bot", "chat_status"]) ||
        !row(body.bot) ||
        body.bot.bot_id !== botId ||
        !exact(body.chat_status, ["state", "blockers"]) ||
        !member(body.chat_status.state, [
          "unprepared",
          "prepared",
          "reopenable",
          "archived",
          "transcript_missing",
          "blocked",
          "unavailable",
        ]) ||
        !codes(body.chat_status.blockers)
      )
        return invalid("invalid_response");
      return body as unknown as BotDetail;
    },
    async threads(botId, signal) {
      if (!uuid(botId)) return invalid("invalid_input");
      const { body, status } = await request(`${base}/${botId}/threads`, "GET", undefined, signal);
      if (
        status !== 200 ||
        !exact(body, ["bot_id", "threads", "has_more"]) ||
        body.bot_id !== botId ||
        typeof body.has_more !== "boolean" ||
        !Array.isArray(body.threads) ||
        body.threads.length > 50 ||
        !body.threads.every(
          (item) =>
            exact(item, [
              "session_id",
              "profile_revision",
              "created_at",
              "started",
              "title",
              "archived",
            ]) &&
            uuid(item.session_id) &&
            revision(item.profile_revision) &&
            stamp(item.created_at) &&
            typeof item.started === "boolean" &&
            typeof item.archived === "boolean" &&
            text(item.title, 1024, 0) &&
            [...(item.title as string)].length <= 256 &&
            ![...(item.title as string)].some((character) => character.charCodeAt(0) < 32),
        ) ||
        new Set(body.threads.map((item) => item.session_id)).size !== body.threads.length
      )
        return invalid("invalid_response");
      return body as unknown as BotThreads;
    },
    async create(botId, profileId, expectedProfileRevision, workspaceId, signal) {
      if (![botId, profileId, workspaceId].every(uuid) || !revision(expectedProfileRevision))
        return invalid("invalid_input");
      const { body, status } = await request(
        base,
        "POST",
        {
          bot_id: botId,
          profile_id: profileId,
          expected_profile_revision: expectedProfileRevision,
          workspace_id: workspaceId,
        },
        signal,
      );
      if (
        (status !== 200 && status !== 201) ||
        !exact(body, ["bot", "replayed"]) ||
        !row(body.bot) ||
        typeof body.replayed !== "boolean" ||
        body.bot.bot_id !== botId ||
        body.bot.profile_id !== profileId ||
        body.bot.workspace_id !== workspaceId
      )
        return invalid("invalid_response");
      return { bot: body.bot, replayed: body.replayed };
    },
    async chat(bot, signal) {
      const {
        bot_id: botId,
        profile_id: profileId,
        workspace_id: workspaceId,
        chat_session_id: sessionId,
        state_revision: stateRevision,
      } = bot;
      if (![botId, profileId, workspaceId, sessionId].every(uuid) || !revision(stateRevision))
        return invalid("invalid_input");
      const { body, status } = await request(
        `${base}/${botId}/chat`,
        "POST",
        { expected_state_revision: stateRevision },
        signal,
        true,
      );
      if (
        (status !== 200 && status !== 422) ||
        !exact(body, ["bot_id", "state_revision", "preview"]) ||
        body.bot_id !== botId ||
        body.state_revision !== stateRevision
      )
        return invalid("invalid_response");
      const preview = parseProfilePreview(body.preview, status);
      if (
        !preview ||
        preview.profile_id !== profileId ||
        ("bot_memory" in preview && preview.bot_memory?.bot_id !== botId) ||
        ("session_id" in preview &&
          (preview.session_id !== sessionId || preview.workspace_id !== workspaceId))
      )
        return invalid("invalid_response");
      return { bot_id: botId, state_revision: stateRevision, preview };
    },
    async newChat(botId, expectedStateRevision, signal) {
      mutation(botId, expectedStateRevision);
      const result = await request(
        `${base}/${botId}/new-chat`,
        "POST",
        { expected_state_revision: expectedStateRevision },
        signal,
      );
      return changed(result.body, result.status, botId);
    },
    async setDelivery(botId, expectedStateRevision, enabled, signal) {
      mutation(botId, expectedStateRevision);
      if (typeof enabled !== "boolean") return invalid("invalid_input");
      const result = await request(
        `${base}/${botId}/delivery`,
        "POST",
        { expected_state_revision: expectedStateRevision, enabled },
        signal,
      );
      return changed(result.body, result.status, botId);
    },
    async setCodeFolder(botId, expectedStateRevision, workspaceId, signal) {
      mutation(botId, expectedStateRevision);
      if (workspaceId !== null && !uuid(workspaceId)) return invalid("invalid_input");
      const { body, status } = await request(
        `${base}/${botId}/code-folder`,
        "POST",
        { expected_state_revision: expectedStateRevision, workspace_id: workspaceId },
        signal,
      );
      const result = changed(body, status, botId);
      const granted = result.bot.code_folder;
      // The answer is the saved row: it must show the requested grant at a newer revision.
      if (
        result.bot.state_revision <= expectedStateRevision ||
        granted === undefined ||
        (granted?.workspace_id ?? null) !== workspaceId
      )
        return invalid("invalid_response");
      return result;
    },
    async remove(botId, expectedStateRevision, signal) {
      mutation(botId, expectedStateRevision);
      const { body, status } = await request(
        `${base}/${botId}`,
        "DELETE",
        { expected_state_revision: expectedStateRevision },
        signal,
      );
      if (
        status !== 200 ||
        !exact(body, ["removed", "bot_id", "paused_routine_ids"]) ||
        body.removed !== true ||
        body.bot_id !== botId ||
        !ids(body.paused_routine_ids)
      )
        return invalid("invalid_response");
      return { removed: true, bot_id: botId, paused_routine_ids: body.paused_routine_ids };
    },
    async reset(workspaceId, signal) {
      if (!uuid(workspaceId)) return invalid("invalid_input");
      const { body, status } = await request(
        `${base}/reset`,
        "POST",
        { workspace_id: workspaceId },
        signal,
      );
      if (
        status !== 200 ||
        !exact(body, ["workspace_id", "removed_bot_ids", "paused_routine_ids"]) ||
        body.workspace_id !== workspaceId ||
        !ids(body.removed_bot_ids) ||
        !ids(body.paused_routine_ids)
      )
        return invalid("invalid_response");
      return {
        workspace_id: workspaceId,
        removed_bot_ids: body.removed_bot_ids,
        paused_routine_ids: body.paused_routine_ids,
      };
    },
  };
}
