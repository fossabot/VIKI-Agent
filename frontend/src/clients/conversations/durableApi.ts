/** Authenticated, bounded access to the durable draft, turn-command and approval services. */
import type { NativeRequest, SnapshotItem } from "../../protocol";
import type { VaultExcerptPart } from "../../state/excerptApi";
import { type Attachment, parseAttachments } from "../../turnInput";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
/** The service's own ceilings: `EXCERPTS_PER_TURN`, and a line number of at most 7 digits. */
const MAX_PARTS = 8;
const MAX_LINE = 9_999_999;
const MAX_REVISION = Number.MAX_SAFE_INTEGER;
const MAX_TEXT_BYTES = 1 << 20;
const MAX_DRAFT_BODY_BYTES = (6 << 20) + 1024;
const MAX_COMMAND_BODY_BYTES = 2048;
/** The approval service's own intake ceiling: a 64 KiB choice plus its envelope. */
const MAX_APPROVAL_BODY_BYTES = 65_536 + 1024;
const MAX_TOOL_USE_ID = 256;
const DEFAULT_TIMEOUT_MS = 15_000;
const encoder = new TextEncoder();
const COMMAND_REASONS = new Set([
  "native_unconfirmed",
  "start_failed",
  "runtime_changed",
  "cancelled",
  "send_failed",
  "internal",
  "owner_changed",
  "unsupported_attachment",
  "attachment_unavailable",
  "excerpt_with_command",
  "excerpt_wrong_vault",
  "excerpt_stale",
  "excerpt_unavailable",
  "excerpts_too_large",
]);
const APPROVAL_STATES = ["recorded", "delivering", "delivered_locally", "not_delivered", "unknown"];
const APPROVAL_REASONS = new Set([
  "stale_request",
  "runtime_changed",
  "cancelled",
  "storage",
  "interrupted",
  "owner_changed",
  "internal",
]);

export interface Draft {
  key: string;
  revision: number;
  text: string;
  saved_at: string | null;
  attachments?: readonly Attachment[];
  parts?: readonly VaultExcerptPart[];
}

export interface DraftSummary {
  key: string;
  revision: number;
  saved_at: string;
  bytes: number;
}

export interface DraftPage {
  drafts: DraftSummary[];
  next_after: string | null;
}

export interface TargetDescriptor {
  session_id: string;
  generation: string;
  closed: boolean;
  busy: boolean;
  live: boolean;
}

export interface CommandInput {
  command_id: string;
  session_id: string;
  generation: string;
  draft_revision: number;
}

export type CommandState = "accepted" | "dispatching" | "unknown" | "not_dispatched";

export interface CommandReceipt extends CommandInput {
  state: CommandState;
  reason: string;
  created_at: string;
  updated_at: string;
  bytes: number;
}

export interface CommandSubmission extends CommandReceipt {
  text: string;
  attachments?: readonly Attachment[];
  parts?: readonly VaultExcerptPart[];
}

export interface CommandPage {
  commands: CommandReceipt[];
  next_after: string | null;
}

export interface PageOptions {
  after?: string;
  limit?: number;
}

/** What the ledger proves about one recorded choice. Only `delivered_locally` says the driver's
 * own future resolved with it; none of them says claude acted on it. */
export type ApprovalState =
  | "recorded"
  | "delivering"
  | "delivered_locally"
  | "not_delivered"
  | "unknown";

export type ApprovalChoice =
  | { kind: "permission"; allow: boolean }
  | { kind: "question"; answers: Record<string, string>; response: string };

export interface ApprovalInput {
  choice_id: string;
  session_id: string;
  generation: string;
  tool_use_id: string;
  native_request: NativeRequest;
  choice: ApprovalChoice;
}

/** Metadata only: the user's answer itself is never in a receipt, only behind `/choice`. */
export interface ApprovalReceipt {
  choice_id: string;
  session_id: string;
  generation: string;
  runtime_id: string;
  request_id: string;
  turn_id: string;
  tool_use_id: string;
  state: ApprovalState;
  reason: string;
  created_at: string;
  updated_at: string;
}

export interface ApprovalRecord extends ApprovalReceipt {
  choice: ApprovalChoice;
  /** The card as it was displayed when the choice was recorded, captured by the server. */
  request: SnapshotItem;
}

export interface ApprovalPage {
  approvals: ApprovalReceipt[];
  next_after: string | null;
}

export interface DurableApi {
  getDraft(key: string): Promise<Draft>;
  listDrafts(options?: PageOptions): Promise<DraftPage>;
  saveDraft(
    key: string,
    expectedRevision: number,
    text: string,
    attachments?: readonly Attachment[],
    parts?: readonly VaultExcerptPart[],
  ): Promise<Draft>;
  clearDraft(key: string, expectedRevision: number): Promise<Draft>;
  getTarget(sessionId: string): Promise<TargetDescriptor>;
  /** Read-only exact draft-revision recovery after browser command metadata is lost. */
  getDraftCommand(sessionId: string, revision: number): Promise<CommandReceipt | null>;
  submitCommand(input: CommandInput): Promise<CommandReceipt>;
  getCommand(commandId: string): Promise<CommandReceipt>;
  listCommands(options?: PageOptions): Promise<CommandPage>;
  getSubmission(commandId: string): Promise<CommandSubmission>;
  /** Read-only lookup of the one choice recorded for a native request, before allocating an ID. */
  findApproval(
    sessionId: string,
    runtimeId: string,
    requestId: string,
  ): Promise<ApprovalReceipt | null>;
  recordApproval(input: ApprovalInput): Promise<ApprovalReceipt>;
  getApproval(choiceId: string): Promise<ApprovalReceipt>;
  getApprovalChoice(choiceId: string): Promise<ApprovalRecord>;
  listApprovals(options?: PageOptions): Promise<ApprovalPage>;
}

export interface DurableApiOptions {
  fetch?: typeof fetch;
  timeoutMs?: number;
}

/** Safe, fixed client message; server response bodies and transport exceptions stay private. */
export class DurableApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly currentRevision?: number,
  ) {
    super("The durable operation could not be completed.");
    this.name = "DurableApiError";
  }
}

function invalidInput(): never {
  throw new DurableApiError(0, "invalid_input");
}

function invalidResponse(): never {
  throw new DurableApiError(0, "invalid_response");
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) invalidResponse();
  return value as Record<string, unknown>;
}

function uuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

function draftKey(value: unknown): value is string {
  return value === "new" || uuid(value);
}

function revision(value: unknown, minimum: number): value is number {
  return (
    Number.isSafeInteger(value) && (value as number) >= minimum && (value as number) <= MAX_REVISION
  );
}

function line(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= MAX_LINE;
}

function timestamp(value: unknown): value is string {
  return typeof value === "string" && TIMESTAMP.test(value);
}

function bytes(value: unknown, minimum: number): value is number {
  return (
    Number.isSafeInteger(value) &&
    (value as number) >= minimum &&
    (value as number) <= MAX_TEXT_BYTES
  );
}

/** The excerpt parts of one draft or turn, in the shape the service's `part_data` writes them.
 * Validated like `parseAttachments`: a part this window cannot vouch for is a bad response, not a
 * quotation to carry back into the next save. */
function parseParts(value: unknown): readonly VaultExcerptPart[] | null {
  if (!Array.isArray(value) || value.length > MAX_PARTS) return null;
  const parsed: VaultExcerptPart[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") return null;
    const row = item as Record<string, unknown>;
    const start = row.start_line;
    const end = row.end_line;
    if (
      row.kind !== "vault_excerpt" ||
      !uuid(row.vault_id) ||
      typeof row.ref !== "string" ||
      row.ref === "" ||
      !line(start) ||
      !line(end) ||
      start > end ||
      typeof row.sha256 !== "string" ||
      !SHA256.test(row.sha256)
    )
      return null;
    parsed.push({
      kind: "vault_excerpt",
      vault_id: row.vault_id,
      ref: row.ref,
      start_line: start,
      end_line: end,
      sha256: row.sha256,
    });
  }
  return parsed;
}

function parseDraft(value: unknown, key: string): Draft {
  const row = object(value);
  if (
    row.key !== key ||
    !revision(row.revision, 0) ||
    typeof row.text !== "string" ||
    (row.attachments !== undefined && parseAttachments(row.attachments) === null) ||
    (row.parts !== undefined && parseParts(row.parts) === null) ||
    encoder.encode(row.text).byteLength > MAX_TEXT_BYTES ||
    !(row.saved_at === null || timestamp(row.saved_at)) ||
    (row.revision === 0 &&
      (row.text !== "" ||
        row.saved_at !== null ||
        (Array.isArray(row.attachments) && row.attachments.length > 0) ||
        (Array.isArray(row.parts) && row.parts.length > 0))) ||
    (row.revision > 0 && row.saved_at === null)
  )
    invalidResponse();
  return row as unknown as Draft;
}

function parseDraftSummary(value: unknown): DraftSummary {
  const row = object(value);
  if (
    !draftKey(row.key) ||
    !revision(row.revision, 1) ||
    !timestamp(row.saved_at) ||
    !bytes(row.bytes, 0) ||
    "text" in row
  )
    invalidResponse();
  return row as unknown as DraftSummary;
}

function parseReceipt(value: unknown, commandId?: string, withText = false): CommandReceipt {
  const row = object(value);
  if (
    !uuid(row.command_id) ||
    (commandId !== undefined && row.command_id !== commandId) ||
    !uuid(row.session_id) ||
    !uuid(row.generation) ||
    !revision(row.draft_revision, 1) ||
    !["accepted", "dispatching", "unknown", "not_dispatched"].includes(row.state as string) ||
    typeof row.reason !== "string" ||
    (row.reason !== "" && !COMMAND_REASONS.has(row.reason)) ||
    ((row.state === "accepted" || row.state === "dispatching") && row.reason !== "") ||
    ((row.state === "unknown" || row.state === "not_dispatched") && row.reason === "") ||
    !timestamp(row.created_at) ||
    !timestamp(row.updated_at) ||
    !bytes(row.bytes, 0) ||
    (!withText && "text" in row)
  )
    invalidResponse();
  return row as unknown as CommandReceipt;
}

/** Bounded only, exactly like the DDL's `length(tool_use_id) <= 256`. "" is a real card: the
 * driver builds one with `ctx.tool_use_id or ""`, and refusing it here would leave that card
 * answerable-looking and unanswerable, with no socket path left to answer it by. */
function toolUseId(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_TOOL_USE_ID;
}

/** Exactly one of the two shapes the service accepts; an extra key means the body is not ours. */
function approvalChoice(value: unknown): ApprovalChoice | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const keys = Object.keys(row);
  if (row.kind === "permission" && typeof row.allow === "boolean" && keys.length === 2)
    return { kind: "permission", allow: row.allow };
  if (
    row.kind === "question" &&
    typeof row.response === "string" &&
    keys.length === 3 &&
    row.answers !== null &&
    typeof row.answers === "object" &&
    !Array.isArray(row.answers) &&
    Object.values(row.answers as Record<string, unknown>).every((each) => typeof each === "string")
  )
    return {
      kind: "question",
      answers: row.answers as Record<string, string>,
      response: row.response,
    };
  return null;
}

/** `withChoice` is the approval analogue of the command receipt's `text` rule: a metadata read
 * that carries the user's decrypted answer is refused rather than rendered. */
function parseApprovalReceipt(
  value: unknown,
  choiceId?: string,
  withChoice = false,
): ApprovalReceipt {
  const row = object(value);
  // Only these two states carry a reason, and they always carry one (the ledger's state table).
  const explained = row.state === "not_delivered" || row.state === "unknown";
  if (
    !uuid(row.choice_id) ||
    (choiceId !== undefined && row.choice_id !== choiceId) ||
    !uuid(row.session_id) ||
    !uuid(row.generation) ||
    !uuid(row.runtime_id) ||
    !uuid(row.request_id) ||
    !uuid(row.turn_id) ||
    !toolUseId(row.tool_use_id) ||
    !APPROVAL_STATES.includes(row.state as string) ||
    typeof row.reason !== "string" ||
    (row.reason !== "" && !APPROVAL_REASONS.has(row.reason)) ||
    (explained ? row.reason === "" : row.reason !== "") ||
    !timestamp(row.created_at) ||
    !timestamp(row.updated_at) ||
    (!withChoice && ("choice" in row || "request" in row))
  )
    invalidResponse();
  return row as unknown as ApprovalReceipt;
}

function pagePath(
  path: string,
  options: PageOptions | undefined,
  key: (value: unknown) => boolean,
): string {
  if (options?.after !== undefined && !key(options.after)) invalidInput();
  if (
    options?.limit !== undefined &&
    (!Number.isInteger(options.limit) || options.limit < 1 || options.limit > 100)
  )
    invalidInput();
  const query = new URLSearchParams();
  if (options?.after !== undefined) query.set("after", options.after);
  if (options?.limit !== undefined) query.set("limit", String(options.limit));
  return query.size > 0 ? `${path}?${query}` : path;
}

const SERVER_CODES: Record<number, ReadonlySet<string>> = {
  400: new Set([
    "invalid_key",
    "invalid_revision",
    "invalid_text",
    "invalid_page",
    "invalid_body",
    "invalid_input",
    "invalid_id",
    "invalid_command",
    "invalid_choice",
    "empty_draft",
    "disconnected",
    "unsupported_attachment",
    "invalid_attachment",
  ]),
  401: new Set(["unauthorized"]),
  403: new Set(["forbidden_origin"]),
  404: new Set(["not_found", "no_target"]),
  408: new Set(["intake_timeout"]),
  409: new Set([
    "conflict",
    "draft_conflict",
    "draft_already_submitted",
    "already_decided",
    "stale_generation",
    "stale_request",
    "closed",
    "busy",
    "turn_running",
    "attachment_unavailable",
  ]),
  413: new Set(["too_large"]),
  415: new Set(["invalid_representation"]),
  429: new Set(["capacity", "overloaded"]),
  503: new Set(["closing", "protection", "corrupt", "unsafe_path", "unavailable", "internal"]),
};

async function responseError(response: Response): Promise<DurableApiError> {
  try {
    const error = object((await response.json()).error);
    const code =
      typeof error.code === "string" && SERVER_CODES[response.status]?.has(error.code)
        ? error.code
        : "http_error";
    const currentRevision =
      response.status === 409 && code === "conflict" && revision(error.current_revision, 0)
        ? error.current_revision
        : undefined;
    return new DurableApiError(response.status, code, currentRevision);
  } catch {
    return new DurableApiError(response.status, "http_error");
  }
}

export function createDurableApi(token: string, options: DurableApiOptions = {}): DurableApi {
  const fetchImpl = options.fetch ?? fetch;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) invalidInput();

  async function request(
    path: string,
    method: "GET" | "PUT" | "POST",
    body?: string,
  ): Promise<unknown> {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new DurableApiError(0, "timeout"));
      }, timeoutMs);
    });
    const operation = (async () => {
      let response: Response;
      try {
        response = await fetchImpl(path, {
          method,
          headers: {
            authorization: `Bearer ${token}`,
            ...(body === undefined ? {} : { "content-type": "application/json" }),
          },
          ...(body === undefined ? {} : { body }),
          signal: controller.signal,
        });
      } catch {
        throw new DurableApiError(0, "network_error");
      }
      if (!response.ok) throw await responseError(response);
      if (response.status !== 200 && !(method === "POST" && response.status === 202))
        invalidResponse();
      try {
        return (await response.json()) as unknown;
      } catch {
        return invalidResponse();
      }
    })();
    try {
      return await Promise.race([operation, timeout]);
    } finally {
      clearTimeout(timer);
    }
  }

  async function saveDraft(
    key: string,
    expectedRevision: number,
    text: string,
    attachments?: readonly Attachment[],
    parts?: readonly VaultExcerptPart[],
  ): Promise<Draft> {
    if (!draftKey(key) || !revision(expectedRevision, 0) || typeof text !== "string")
      invalidInput();
    if (encoder.encode(text).byteLength > MAX_TEXT_BYTES) throw new DurableApiError(0, "too_large");
    if (attachments !== undefined && parseAttachments(attachments) === null) invalidInput();
    if (parts !== undefined && parseParts(parts) === null) invalidInput();
    // An omitted `parts` is the service's legacy text-only save, which clears the stored
    // excerpts; the key is sent only when this save carries parts (`parts` field omitted when
    // the caller passes none).
    const body = JSON.stringify({
      expected_revision: expectedRevision,
      text,
      ...(attachments === undefined ? {} : { attachments }),
      ...(parts === undefined || parts.length === 0 ? {} : { parts }),
    });
    if (encoder.encode(body).byteLength > MAX_DRAFT_BODY_BYTES)
      throw new DurableApiError(0, "too_large");
    const draft = parseDraft(await request(`/api/v2/drafts/${key}`, "PUT", body), key);
    if (
      draft.text !== text ||
      JSON.stringify(draft.attachments ?? []) !== JSON.stringify(attachments ?? []) ||
      JSON.stringify(draft.parts ?? []) !== JSON.stringify(parts ?? []) ||
      draft.revision < expectedRevision ||
      draft.revision > expectedRevision + 1
    )
      invalidResponse();
    return draft;
  }

  return {
    async getDraft(key) {
      if (!draftKey(key)) invalidInput();
      return parseDraft(await request(`/api/v2/drafts/${key}`, "GET"), key);
    },
    async listDrafts(options) {
      const row = object(await request(pagePath("/api/v2/drafts", options, draftKey), "GET"));
      if (!Array.isArray(row.drafts) || row.drafts.length > (options?.limit ?? 50))
        invalidResponse();
      const drafts = row.drafts.map(parseDraftSummary);
      const after = options?.after ?? "";
      if (drafts.some((draft, i) => draft.key <= (i === 0 ? after : (drafts[i - 1]?.key ?? after))))
        invalidResponse();
      if (
        !(row.next_after === null || (drafts.length > 0 && row.next_after === drafts.at(-1)?.key))
      )
        invalidResponse();
      return { drafts, next_after: row.next_after as string | null };
    },
    saveDraft,
    clearDraft: (key, expectedRevision) => saveDraft(key, expectedRevision, ""),
    async getTarget(sessionId) {
      if (!uuid(sessionId)) invalidInput();
      const row = object(await request(`/api/v2/sessions/${sessionId}/target`, "GET"));
      if (
        row.session_id !== sessionId ||
        !uuid(row.generation) ||
        typeof row.closed !== "boolean" ||
        typeof row.busy !== "boolean" ||
        typeof row.live !== "boolean"
      )
        invalidResponse();
      return row as unknown as TargetDescriptor;
    },
    async getDraftCommand(sessionId, draftRevision) {
      if (!uuid(sessionId) || !revision(draftRevision, 1)) invalidInput();
      try {
        const row = parseReceipt(
          await request(
            `/api/v2/sessions/${sessionId}/draft-command?revision=${draftRevision}`,
            "GET",
          ),
        );
        if (row.session_id !== sessionId || row.draft_revision !== draftRevision) invalidResponse();
        return row;
      } catch (error) {
        if (error instanceof DurableApiError && error.status === 404 && error.code === "not_found")
          return null;
        throw error;
      }
    },
    async submitCommand(input) {
      if (
        !uuid(input.command_id) ||
        !uuid(input.session_id) ||
        !uuid(input.generation) ||
        !revision(input.draft_revision, 1)
      )
        invalidInput();
      const body = JSON.stringify({
        command_id: input.command_id,
        session_id: input.session_id,
        generation: input.generation,
        draft_revision: input.draft_revision,
      });
      if (encoder.encode(body).byteLength > MAX_COMMAND_BODY_BYTES)
        throw new DurableApiError(0, "too_large");
      const receipt = parseReceipt(
        await request("/api/v2/commands", "POST", body),
        input.command_id,
      );
      if (
        receipt.session_id !== input.session_id ||
        receipt.generation !== input.generation ||
        receipt.draft_revision !== input.draft_revision
      )
        invalidResponse();
      return receipt;
    },
    async getCommand(commandId) {
      if (!uuid(commandId)) invalidInput();
      return parseReceipt(await request(`/api/v2/commands/${commandId}`, "GET"), commandId);
    },
    async listCommands(options) {
      const row = object(await request(pagePath("/api/v2/commands", options, uuid), "GET"));
      if (!Array.isArray(row.commands) || row.commands.length > (options?.limit ?? 50))
        invalidResponse();
      const commands = row.commands.map((item) => parseReceipt(item));
      const after = options?.after ?? "";
      if (
        commands.some(
          (command, i) =>
            command.command_id <= (i === 0 ? after : (commands[i - 1]?.command_id ?? after)),
        )
      )
        invalidResponse();
      if (
        !(
          row.next_after === null ||
          (commands.length > 0 && row.next_after === commands.at(-1)?.command_id)
        )
      )
        invalidResponse();
      return { commands, next_after: row.next_after as string | null };
    },
    async getSubmission(commandId) {
      if (!uuid(commandId)) invalidInput();
      const row = object(await request(`/api/v2/commands/${commandId}/submission`, "GET"));
      const receipt = parseReceipt(row, commandId, true);
      if (typeof row.text !== "string" || encoder.encode(row.text).byteLength !== receipt.bytes)
        invalidResponse();
      if (row.attachments !== undefined && parseAttachments(row.attachments) === null)
        invalidResponse();
      if (row.parts !== undefined && parseParts(row.parts) === null) invalidResponse();
      return row as unknown as CommandSubmission;
    },
    async findApproval(sessionId, runtimeId, requestId) {
      if (!uuid(sessionId) || !uuid(runtimeId) || !uuid(requestId)) invalidInput();
      const query = new URLSearchParams({ runtime_id: runtimeId, request_id: requestId });
      try {
        const receipt = parseApprovalReceipt(
          await request(`/api/v2/sessions/${sessionId}/approval?${query}`, "GET"),
        );
        if (
          receipt.session_id !== sessionId ||
          receipt.runtime_id !== runtimeId ||
          receipt.request_id !== requestId
        )
          invalidResponse();
        return receipt;
      } catch (error) {
        // Only an authenticated, explicit "no such row" is an answer; anything else stays a fault.
        if (error instanceof DurableApiError && error.status === 404 && error.code === "not_found")
          return null;
        throw error;
      }
    },
    async recordApproval(input) {
      const choice = approvalChoice(input.choice);
      if (
        !uuid(input.choice_id) ||
        !uuid(input.session_id) ||
        !uuid(input.generation) ||
        !toolUseId(input.tool_use_id) ||
        !uuid(input.native_request?.runtime_id) ||
        !uuid(input.native_request?.request_id) ||
        !uuid(input.native_request?.turn_id) ||
        !choice
      )
        invalidInput();
      const body = JSON.stringify({
        choice_id: input.choice_id,
        session_id: input.session_id,
        generation: input.generation,
        tool_use_id: input.tool_use_id,
        native_request: {
          runtime_id: input.native_request.runtime_id,
          request_id: input.native_request.request_id,
          turn_id: input.native_request.turn_id,
        },
        choice,
      });
      if (encoder.encode(body).byteLength > MAX_APPROVAL_BODY_BYTES)
        throw new DurableApiError(0, "too_large");
      const receipt = parseApprovalReceipt(
        await request("/api/v2/approvals", "POST", body),
        input.choice_id,
      );
      if (
        receipt.session_id !== input.session_id ||
        receipt.generation !== input.generation ||
        receipt.tool_use_id !== input.tool_use_id ||
        receipt.runtime_id !== input.native_request.runtime_id ||
        receipt.request_id !== input.native_request.request_id ||
        receipt.turn_id !== input.native_request.turn_id
      )
        invalidResponse();
      return receipt;
    },
    async getApproval(choiceId) {
      if (!uuid(choiceId)) invalidInput();
      return parseApprovalReceipt(await request(`/api/v2/approvals/${choiceId}`, "GET"), choiceId);
    },
    async getApprovalChoice(choiceId) {
      if (!uuid(choiceId)) invalidInput();
      const row = object(await request(`/api/v2/approvals/${choiceId}/choice`, "GET"));
      parseApprovalReceipt(row, choiceId, true);
      const choice = approvalChoice(row.choice);
      const displayed = object(row.request);
      if (!choice || typeof displayed.type !== "string" || displayed.type === "") invalidResponse();
      object(displayed.data);
      return { ...row, choice } as unknown as ApprovalRecord;
    },
    async listApprovals(options) {
      const row = object(await request(pagePath("/api/v2/approvals", options, uuid), "GET"));
      if (!Array.isArray(row.approvals) || row.approvals.length > (options?.limit ?? 50))
        invalidResponse();
      const approvals = row.approvals.map((item) => parseApprovalReceipt(item));
      const after = options?.after ?? "";
      if (
        approvals.some(
          (approval, i) =>
            approval.choice_id <= (i === 0 ? after : (approvals[i - 1]?.choice_id ?? after)),
        )
      )
        invalidResponse();
      if (
        !(
          row.next_after === null ||
          (approvals.length > 0 && row.next_after === approvals.at(-1)?.choice_id)
        )
      )
        invalidResponse();
      return { approvals, next_after: row.next_after as string | null };
    },
  };
}
