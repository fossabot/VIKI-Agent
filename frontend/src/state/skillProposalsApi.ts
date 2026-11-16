import { boundedJson } from "../clients/boundedResponse";

const MAX = 8 * 1024 * 1024;
const TIMEOUT = 15_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SKILL_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
// Every timestamp the server sends is its 24-character UTC form, a string.
const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

export type SkillState =
  | "open"
  | "handed"
  | "applied"
  | "rejected"
  | "stale"
  | "source_missing"
  | "unknown";
export type SkillTrigger = "button" | "long_session" | "repeated_request" | "correction";
export type SkillPolicy = "ordinary" | "existing_readonly" | "protected";
export type SkillProposalAction = "approve" | "reject" | "edit" | "withdraw";

export interface SkillProposalItem {
  kind: "skill_proposal";
  id: string;
  origin: { workspace_id: string; session_id: null };
  revision: string;
  state: SkillState;
  attention: boolean;
  title: string;
  name: string;
  relative_id: string;
  trigger: SkillTrigger;
  sessions: string[];
  base_revision: string | null;
  text_bytes: number;
  approved_at: string | null;
  rejected_at: string | null;
  reason: string | null;
  created_at: string;
  updated_at: string;
}
export interface SkillProposalDetail extends SkillProposalItem {
  text: string;
  current_text: string | null;
  diff: string;
  diff_reason: "no_source" | "too_large" | "unreadable" | null;
  description: string;
  unsupported_fields: string[];
  shadowed: boolean | null;
  policy: SkillPolicy;
}
export interface CreatedSkillProposal {
  id: string;
  /** The stored proposal; null when the server replayed an identical earlier create. */
  item: SkillProposalItem | null;
  replayed: boolean;
}

export const SKILL_STATE_WORDS: Record<SkillState, string> = {
  open: "Proposed",
  handed: "Approved, not applied yet",
  applied: "Skill file matches the proposal",
  rejected: "Rejected",
  stale: "Skill changed since",
  source_missing: "Skill file not found",
  unknown: "State unknown",
};
export const SKILL_TRIGGER_WORDS: Record<SkillTrigger, string> = {
  button: "You asked for it",
  long_session: "A long task finished",
  repeated_request: "The same request in several chats",
  correction: "Your correction in a chat",
};

/** The server's error code and its `details` (ids, revisions, limits), or a local code. */
export class SkillProposalsApiError extends Error {
  constructor(
    readonly code: string,
    readonly extra: Record<string, unknown> = {},
  ) {
    super(code);
  }
}

// The words for `invalid_skill`'s `reason` detail (native_assets.parse_skill_text's rejections).
const REASON_WORDS: Record<string, string> = {
  missing_frontmatter: "it needs frontmatter between --- lines at the top",
  malformed_metadata: "its frontmatter is not in a form VIKI Agent reads",
  missing_name: "its frontmatter has no name",
  invalid_name:
    "its name must be lowercase letters, digits and single hyphens, at most 64 characters",
  reserved_name: "Claude Code reserves that name",
  missing_description: "its frontmatter has no description",
  long_description: "its description is over 1,536 characters",
  carriage_return: "it holds characters VIKI Agent does not keep",
  control_character: "it holds characters VIKI Agent does not keep",
  invalid_encoding: "it holds characters VIKI Agent does not keep",
};
const OTHER_REASON = "its file is not in the expected form";
const REFUSALS: Record<string, string> = {
  too_large: "This skill is over 128 KiB.",
  no_vault: "Open a vault to propose skills.",
  not_active: "Open this proposal's vault to act on it.",
  catalog_partial: "VIKI Agent could not read this vault's skills in full. Try again.",
  no_change: "The vault already has this exact skill.",
  proposal_conflict:
    "Approvals already holds a proposal for this skill. Open it to edit it, or withdraw it first.",
  proposals_full:
    "Approvals already keeps 100 skill proposals for this vault. Withdraw some first.",
  stale_revision: "This proposal changed since you opened it. Open it again.",
  not_approvable: "This proposal cannot be approved in its current state.",
  already_applied: "The skill file already matches this proposal.",
  name_changed: "An edit keeps the skill's name. Propose a new skill for a new name.",
  unknown_proposal: "This proposal is gone.",
};
const FALLBACK_REFUSAL = "That did not work. Try again.";

/** The refusal line for `error`'s code; a generic line for anything else. */
export function skillRefusal(error: unknown): string {
  if (!(error instanceof SkillProposalsApiError)) return FALLBACK_REFUSAL;
  if (error.code === "invalid_skill") {
    const reason = error.extra.reason;
    const words = (typeof reason === "string" && REASON_WORDS[reason]) || OTHER_REASON;
    return `This is not a skill VIKI Agent can propose: ${words}.`;
  }
  return REFUSALS[error.code] ?? FALLBACK_REFUSAL;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const isString = (value: unknown): value is string => typeof value === "string";
const uuid = (value: unknown): value is string => isString(value) && UUID.test(value);
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

const SKILL_STATES: readonly string[] = Object.keys(SKILL_STATE_WORDS);
const SKILL_TRIGGERS: readonly string[] = Object.keys(SKILL_TRIGGER_WORDS);
const SKILL_POLICIES: readonly string[] = ["ordinary", "existing_readonly", "protected"];
const DIFF_REASONS: readonly string[] = ["no_source", "too_large", "unreadable"];

export function isSkillProposalItem(value: unknown): value is SkillProposalItem {
  const item = record(value);
  return (
    !!item &&
    item.kind === "skill_proposal" &&
    uuid(item.id) &&
    origin(item.origin) &&
    hex(item.revision, 64) &&
    oneOf(item.state, SKILL_STATES) &&
    typeof item.attention === "boolean" &&
    isString(item.title) &&
    isString(item.name) &&
    SKILL_NAME.test(item.name) &&
    item.name.length <= 64 &&
    item.relative_id === `skills/${item.name}/SKILL.md` &&
    oneOf(item.trigger, SKILL_TRIGGERS) &&
    Array.isArray(item.sessions) &&
    item.sessions.length >= 1 &&
    item.sessions.length <= 10 &&
    item.sessions.every(uuid) &&
    (item.base_revision === null || hex(item.base_revision, 64)) &&
    typeof item.text_bytes === "number" &&
    Number.isInteger(item.text_bytes) &&
    item.text_bytes >= 1 &&
    item.text_bytes <= 131072 &&
    nullableStamp(item.approved_at) &&
    nullableStamp(item.rejected_at) &&
    (item.reason === null ||
      (isString(item.reason) && item.reason.length >= 1 && item.reason.length <= 200)) &&
    stamp(item.created_at) &&
    stamp(item.updated_at)
  );
}

export interface SkillProposalsApi {
  create(
    workspaceId: string,
    text: string,
    trigger: SkillTrigger,
    sessions: string[],
  ): Promise<CreatedSkillProposal>;
  detail(id: string, signal?: AbortSignal): Promise<SkillProposalDetail>;
  act(
    id: string,
    action: SkillProposalAction,
    revision: string,
    text?: string,
    reason?: string,
  ): Promise<SkillProposalItem | null>;
}

export function createSkillProposalsApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): SkillProposalsApi {
  // No custom Origin: the browser sets it, and the server requires one on a POST.
  async function call(
    route: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    if (signal?.aborted) throw new SkillProposalsApiError("cancelled");
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
        if (response.ok) throw new SkillProposalsApiError("invalid_response");
        throw new SkillProposalsApiError("unavailable");
      }
      if (!response.ok) {
        const error = record(parsed?.error);
        throw new SkillProposalsApiError(
          error && isString(error.code) ? error.code : "unavailable",
          record(error?.details) ?? {},
        );
      }
      if (!parsed) throw new SkillProposalsApiError("invalid_response");
      return parsed;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
  }
  const invalid = (): never => {
    throw new SkillProposalsApiError("invalid_response");
  };
  const path = (id: string) => encodeURIComponent(id);
  return {
    async create(workspaceId, text, trigger, sessions) {
      const body = await call(`/api/v2/vaults/${path(workspaceId)}/skill-proposals`, {
        text,
        trigger,
        sessions,
      });
      const found = record(body.proposal);
      if (body.replayed === true && found && isString(found.id))
        return { id: found.id, item: null, replayed: true };
      return isSkillProposalItem(found)
        ? { id: found.id, item: found, replayed: false }
        : invalid();
    },
    async detail(id, signal) {
      const body = await call(`/api/v2/decisions/skills/${path(id)}`, undefined, signal);
      const found = record(body.proposal);
      if (
        !isSkillProposalItem(found) ||
        !isString(found.text) ||
        !(found.current_text === null || isString(found.current_text)) ||
        !isString(found.diff) ||
        !(found.diff_reason === null || oneOf(found.diff_reason, DIFF_REASONS)) ||
        !isString(found.description) ||
        !(Array.isArray(found.unsupported_fields) && found.unsupported_fields.every(isString)) ||
        !(found.shadowed === null || typeof found.shadowed === "boolean") ||
        !oneOf(found.policy, SKILL_POLICIES)
      )
        return invalid();
      return found as unknown as SkillProposalDetail;
    },
    async act(id, action, revision, text, reason) {
      const body = await call(`/api/v2/decisions/skills/${path(id)}`, {
        action,
        revision,
        ...(text === undefined ? {} : { text }),
        ...(reason === undefined ? {} : { reason }),
      });
      if (action === "withdraw") return body.withdrawn === true ? null : invalid();
      return isSkillProposalItem(body.proposal) ? body.proposal : invalid();
    },
  };
}
