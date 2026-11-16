import { boundedJson } from "../clients/boundedResponse";
import { isSkillProposalItem, type SkillProposalItem } from "./skillProposalsApi";

const MAX = 8 * 1024 * 1024;
const TIMEOUT = 15_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const QUESTION_NAME = /^[a-z_]{1,32}$/;
// Every timestamp the server sends is its 24-character UTC form, a string.
const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

export type VaultProposalState =
  | "open"
  | "handed"
  | "applied"
  | "stale"
  | "source_missing"
  | "unknown";

export interface VaultProposalItem {
  kind: "vault_proposal";
  id: string;
  origin: { workspace_id: string; session_id: null };
  revision: string;
  state: VaultProposalState;
  attention: boolean;
  title: string;
  ref: string;
  origin_kind: string;
  /** Null only for a memory promotion: it creates the note, so no earlier bytes exist. */
  base_revision: string | null;
  text_bytes: number;
  handed_at: string | null;
  created_at: string;
}
export type QuestionProposalState = "open" | "approved" | "replaced" | "rejected";

export interface QuestionProposalItem {
  kind: "question_proposal";
  id: string;
  origin: { workspace_id: string; session_id: null };
  revision: string;
  state: QuestionProposalState;
  attention: boolean;
  title: string;
  name: string;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
  rejected_at: string | null;
  reason: string | null;
}
export type ChangeKind = "note_link" | "note_superseded" | "tasks_ticked";
export type ChangeProposalState = "open" | "approved" | "rejected";

export interface ChangeProposalItem {
  kind: "change_proposal";
  id: string;
  origin: { workspace_id: string; session_id: null };
  revision: string;
  state: ChangeProposalState;
  attention: boolean;
  title: string;
  change: ChangeKind;
  ref: string;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
  rejected_at: string | null;
  reason: string | null;
}
export const CHANGE_STATUS_LABELS: Readonly<Record<ChangeProposalState, string>> = {
  open: "Proposed",
  approved: "Draft made",
  rejected: "Rejected",
};
export interface DecisionInbox {
  items: (VaultProposalItem | SkillProposalItem | QuestionProposalItem | ChangeProposalItem)[];
}
export interface ProposalDetail extends VaultProposalItem {
  current_revision: string | null;
  proposed_text: string;
  diff: string;
  diff_reason: string | null;
}
export interface CreatedProposal {
  id: string;
  /** The stored proposal; null when the server replayed an identical earlier create. */
  item: VaultProposalItem | null;
  replayed: boolean;
}

/** The server's error code and its `details` (ids, revisions, limits), or a local code. */
export class DecisionsApiError extends Error {
  constructor(
    readonly code: string,
    readonly extra: Record<string, unknown> = {},
  ) {
    super(code);
  }
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const isString = (value: unknown): value is string => typeof value === "string";
const nullableString = (value: unknown): boolean => value === null || isString(value);
const nullableStamp = (value: unknown): boolean => value === null || stamp(value);
const stamp = (value: unknown): boolean => isString(value) && STAMP.test(value);
const oneOf = (value: unknown, allowed: readonly string[]): boolean =>
  isString(value) && allowed.includes(value);
const hex = (value: unknown, length: number): boolean =>
  isString(value) && new RegExp(`^[0-9a-f]{${length}}$`).test(value);
const origin = (value: unknown): boolean => {
  const found = record(value);
  return !!found && isString(found.workspace_id) && found.session_id === null;
};
const uuid = (value: unknown): value is string => isString(value) && UUID.test(value);

const PROPOSAL_STATES = ["open", "handed", "applied", "stale", "source_missing", "unknown"];
const QUESTION_STATES = ["open", "approved", "replaced", "rejected"];
const CHANGE_KINDS = ["note_link", "note_superseded", "tasks_ticked"];
const CHANGE_STATES: readonly string[] = Object.keys(CHANGE_STATUS_LABELS);
const CHANGE_PROPOSAL_KEYS = [
  "kind",
  "id",
  "origin",
  "revision",
  "state",
  "attention",
  "title",
  "change",
  "ref",
  "created_at",
  "updated_at",
  "approved_at",
  "rejected_at",
  "reason",
] as const;

function proposalItem(value: unknown): value is VaultProposalItem {
  const item = record(value);
  return (
    !!item &&
    item.kind === "vault_proposal" &&
    isString(item.id) &&
    origin(item.origin) &&
    hex(item.revision, 64) &&
    oneOf(item.state, PROPOSAL_STATES) &&
    typeof item.attention === "boolean" &&
    isString(item.title) &&
    isString(item.ref) &&
    isString(item.origin_kind) &&
    (hex(item.base_revision, 64) ||
      (item.base_revision === null && item.origin_kind === "memory_promotion")) &&
    typeof item.text_bytes === "number" &&
    Number.isInteger(item.text_bytes) &&
    nullableStamp(item.handed_at) &&
    stamp(item.created_at)
  );
}

export function isQuestionProposalItem(value: unknown): value is QuestionProposalItem {
  const item = record(value);
  return (
    !!item &&
    item.kind === "question_proposal" &&
    uuid(item.id) &&
    origin(item.origin) &&
    hex(item.revision, 64) &&
    oneOf(item.state, QUESTION_STATES) &&
    typeof item.attention === "boolean" &&
    isString(item.title) &&
    isString(item.name) &&
    QUESTION_NAME.test(item.name) &&
    stamp(item.created_at) &&
    stamp(item.updated_at) &&
    nullableStamp(item.approved_at) &&
    nullableStamp(item.rejected_at) &&
    (item.reason === null ||
      (isString(item.reason) && item.reason.length >= 1 && item.reason.length <= 200))
  );
}

/** Exactly `CHANGE_PROPOSAL_KEYS`: an extra or a missing key refuses the item, as a bad kind does. */
export function isChangeProposalItem(value: unknown): value is ChangeProposalItem {
  const item = record(value);
  if (!item) return false;
  const keys = Object.keys(item);
  return (
    keys.length === CHANGE_PROPOSAL_KEYS.length &&
    CHANGE_PROPOSAL_KEYS.every((key) => key in item) &&
    item.kind === "change_proposal" &&
    uuid(item.id) &&
    origin(item.origin) &&
    hex(item.revision, 64) &&
    oneOf(item.state, CHANGE_STATES) &&
    typeof item.attention === "boolean" &&
    isString(item.title) &&
    oneOf(item.change, CHANGE_KINDS) &&
    isString(item.ref) &&
    stamp(item.created_at) &&
    stamp(item.updated_at) &&
    nullableStamp(item.approved_at) &&
    nullableStamp(item.rejected_at) &&
    nullableString(item.reason)
  );
}

export interface DecisionsApi {
  decisions(signal?: AbortSignal): Promise<DecisionInbox>;
  proposalDetail(id: string, signal?: AbortSignal): Promise<ProposalDetail>;
  /** The handed-off proposal, or null after a withdraw. */
  proposalAct(id: string, action: "handoff" | "withdraw"): Promise<VaultProposalItem | null>;
  createProposal(
    workspaceId: string,
    ref: string,
    text: string,
    baseRevision: string,
  ): Promise<CreatedProposal>;
}

export function createDecisionsApi(token: string, fetchImpl: typeof fetch = fetch): DecisionsApi {
  // No custom Origin: the browser sets it, and the server requires one on a POST.
  async function call(
    route: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    if (signal?.aborted) throw new DecisionsApiError("cancelled");
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, TIMEOUT);
    try {
      const response = await fetchImpl(route, {
        method: body === undefined ? "GET" : "POST",
        signal: controller.signal,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body === undefined ? {} : { "content-type": "application/json" }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      let parsed: Record<string, unknown> | null;
      try {
        parsed = record(await boundedJson(response, MAX));
      } catch {
        if (response.ok) throw new DecisionsApiError("invalid_response");
        throw new DecisionsApiError("unavailable");
      }
      if (!response.ok) {
        const error = record(parsed?.error);
        throw new DecisionsApiError(
          error && isString(error.code) ? error.code : "unavailable",
          record(error?.details) ?? {},
        );
      }
      if (!parsed) throw new DecisionsApiError("invalid_response");
      return parsed;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const invalid = (): never => {
    throw new DecisionsApiError("invalid_response");
  };
  const path = (id: string) => encodeURIComponent(id);
  return {
    async decisions(signal) {
      const body = await call("/api/v2/decisions", undefined, signal);
      if (
        !Array.isArray(body.items) ||
        !body.items.every(
          (item) =>
            proposalItem(item) ||
            isSkillProposalItem(item) ||
            isQuestionProposalItem(item) ||
            isChangeProposalItem(item),
        )
      )
        return invalid();
      return body as unknown as DecisionInbox;
    },
    async proposalDetail(id, signal) {
      const body = await call(`/api/v2/decisions/proposals/${path(id)}`, undefined, signal);
      const found = record(body.proposal);
      if (
        !proposalItem(found) ||
        !(found.current_revision === null || hex(found.current_revision, 64)) ||
        !isString(found.proposed_text) ||
        !isString(found.diff) ||
        !nullableString(found.diff_reason)
      )
        return invalid();
      return found as unknown as ProposalDetail;
    },
    async proposalAct(id, action) {
      const body = await call(`/api/v2/decisions/proposals/${path(id)}`, { action });
      if (action === "withdraw") return body.withdrawn === true ? null : invalid();
      return proposalItem(body.proposal) ? body.proposal : invalid();
    },
    async createProposal(workspaceId, ref, text, baseRevision) {
      const body = await call(`/api/v2/vaults/${path(workspaceId)}/proposals`, {
        ref,
        text,
        base_revision: baseRevision,
      });
      const found = record(body.proposal);
      if (body.replayed === true && found && isString(found.id))
        return { id: found.id, item: null, replayed: true };
      return proposalItem(found) ? { id: found.id, item: found, replayed: false } : invalid();
    },
  };
}
