/** In-memory fake of the durable draft, command and approval services, for UI tests. Socket
 * fixtures never grant turn authority. */
import {
  type ApprovalInput,
  type ApprovalReceipt,
  type ApprovalRecord,
  type ApprovalState,
  type CommandInput,
  type CommandReceipt,
  type CommandSubmission,
  type Draft,
  type DurableApi,
  DurableApiError,
} from "./clients/conversations/durableApi";
import type { SnapshotItem } from "./protocol";

const AT = "2026-09-14T00:00:00.000Z";
export const FAKE_GENERATION = "11111111-1111-4111-8111-111111111111";

/** One recorded choice, as the ledger holds it: the user's input plus the ledger's own state. */
export interface FakeApproval extends ApprovalInput {
  state: ApprovalState;
  reason: string;
  created_at: string;
  updated_at: string;
}

export interface FakeDurable extends DurableApi {
  drafts: Map<string, Draft>;
  commands: Map<string, CommandSubmission>;
  submissions: CommandInput[];
  saves: { key: string; expectedRevision: number; text: string }[];
  /** Recorded approvals by choice ID, in POST order. */
  approvals: Map<string, FakeApproval>;
}

function receipt(command: CommandSubmission): CommandReceipt {
  const { text: _text, ...metadata } = command;
  return { ...metadata };
}

function approvalReceipt(approval: FakeApproval): ApprovalReceipt {
  return {
    choice_id: approval.choice_id,
    session_id: approval.session_id,
    generation: approval.generation,
    runtime_id: approval.native_request.runtime_id,
    request_id: approval.native_request.request_id,
    turn_id: approval.native_request.turn_id,
    tool_use_id: approval.tool_use_id,
    state: approval.state,
    reason: approval.reason,
    created_at: approval.created_at,
    updated_at: approval.updated_at,
  };
}

/** The displayed projection the real service captures from the actor. The fake never saw a card,
 * so it returns the empty item of the matching type rather than inventing card text. */
function displayedRequest(approval: FakeApproval): SnapshotItem {
  return approval.choice.kind === "permission"
    ? {
        type: "permission.requested",
        data: {
          tool_use_id: approval.tool_use_id,
          kind: "tool",
          tool: "",
          summary: "",
          question: "",
          reason: "",
          agent: "",
          plan: "",
        },
        native_request: approval.native_request,
      }
    : {
        type: "question.asked",
        data: { tool_use_id: approval.tool_use_id, questions: [] },
        native_request: approval.native_request,
      };
}

function sameChoice(a: ApprovalInput["choice"], b: ApprovalInput["choice"]): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function fakeDurable(): FakeDurable {
  const drafts = new Map<string, Draft>();
  const commands = new Map<string, CommandSubmission>();
  const submissions: CommandInput[] = [];
  const saves: FakeDurable["saves"] = [];
  const approvals = new Map<string, FakeApproval>();

  function current(key: string): Draft {
    return { ...(drafts.get(key) ?? { key, revision: 0, text: "", saved_at: null }) };
  }

  async function saveDraft(key: string, expectedRevision: number, text: string): Promise<Draft> {
    saves.push({ key, expectedRevision, text });
    const before = current(key);
    if (expectedRevision !== before.revision) {
      if (expectedRevision + 1 === before.revision && text === before.text) return before;
      throw new DurableApiError(409, "conflict", before.revision);
    }
    if (text === before.text) return before;
    const next: Draft = { key, revision: before.revision + 1, text, saved_at: AT };
    drafts.set(key, next);
    return { ...next };
  }

  return {
    drafts,
    commands,
    submissions,
    saves,
    approvals,
    getDraft: async (key) => current(key),
    async listDrafts(options) {
      const limit = options?.limit ?? 50;
      const rows = [...drafts.values()]
        .filter((draft) => draft.text !== "" && draft.key > (options?.after ?? ""))
        .sort((a, b) => a.key.localeCompare(b.key));
      const page = rows.slice(0, limit);
      return {
        drafts: page.map((draft) => ({
          key: draft.key,
          revision: draft.revision,
          saved_at: draft.saved_at ?? AT,
          bytes: new TextEncoder().encode(draft.text).byteLength,
        })),
        next_after: rows.length > limit ? (page.at(-1)?.key ?? null) : null,
      };
    },
    saveDraft,
    clearDraft: (key, expectedRevision) => saveDraft(key, expectedRevision, ""),
    getTarget: async (sessionId) => ({
      session_id: sessionId,
      generation: FAKE_GENERATION,
      closed: false,
      busy: false,
      live: true,
    }),
    async getDraftCommand(sessionId, draftRevision) {
      const found = [...commands.values()]
        .filter(
          (command) => command.session_id === sessionId && command.draft_revision === draftRevision,
        )
        .sort(
          (a, b) =>
            a.created_at.localeCompare(b.created_at) || a.command_id.localeCompare(b.command_id),
        )[0];
      return found ? receipt(found) : null;
    },
    async submitCommand(input) {
      const immutable = { ...input };
      submissions.push(immutable);
      const existing = commands.get(input.command_id);
      if (existing) {
        if (
          existing.session_id !== input.session_id ||
          existing.generation !== input.generation ||
          existing.draft_revision !== input.draft_revision
        )
          throw new DurableApiError(409, "conflict");
        return receipt(existing);
      }
      if (
        [...commands.values()].some(
          (command) =>
            command.session_id === input.session_id &&
            command.draft_revision === input.draft_revision,
        )
      )
        throw new DurableApiError(409, "draft_already_submitted");
      const draft = current(input.session_id);
      if (draft.revision !== input.draft_revision) throw new DurableApiError(409, "draft_conflict");
      if (draft.text === "") throw new DurableApiError(400, "empty_draft");
      const command: CommandSubmission = {
        ...immutable,
        state: "accepted",
        reason: "",
        created_at: AT,
        updated_at: AT,
        bytes: new TextEncoder().encode(draft.text).byteLength,
        text: draft.text,
      };
      commands.set(input.command_id, command);
      return receipt(command);
    },
    async getCommand(commandId) {
      const command = commands.get(commandId);
      if (!command) throw new DurableApiError(404, "not_found");
      return receipt(command);
    },
    async listCommands(options) {
      const limit = options?.limit ?? 50;
      const rows = [...commands.values()]
        .filter((command) => command.command_id > (options?.after ?? ""))
        .sort((a, b) => a.command_id.localeCompare(b.command_id));
      const page = rows.slice(0, limit);
      return {
        commands: page.map(receipt),
        next_after: rows.length > limit ? (page.at(-1)?.command_id ?? null) : null,
      };
    },
    async getSubmission(commandId) {
      const command = commands.get(commandId);
      if (!command) throw new DurableApiError(404, "not_found");
      return { ...command };
    },
    async findApproval(sessionId, runtimeId, requestId) {
      const found = [...approvals.values()].find(
        (approval) =>
          approval.session_id === sessionId &&
          approval.native_request.runtime_id === runtimeId &&
          approval.native_request.request_id === requestId,
      );
      return found ? approvalReceipt(found) : null;
    },
    async recordApproval(input) {
      const existing = approvals.get(input.choice_id);
      if (existing) {
        // All eight identifiers plus the decoded choice, exactly as the service compares them:
        // one ID may only ever carry one answer to one native request.
        if (
          existing.session_id !== input.session_id ||
          existing.generation !== input.generation ||
          existing.tool_use_id !== input.tool_use_id ||
          existing.native_request.runtime_id !== input.native_request.runtime_id ||
          existing.native_request.request_id !== input.native_request.request_id ||
          existing.native_request.turn_id !== input.native_request.turn_id ||
          !sameChoice(existing.choice, input.choice)
        )
          throw new DurableApiError(409, "conflict");
        return approvalReceipt(existing);
      }
      if (
        [...approvals.values()].some(
          (approval) =>
            approval.session_id === input.session_id &&
            approval.native_request.runtime_id === input.native_request.runtime_id &&
            approval.native_request.request_id === input.native_request.request_id,
        )
      )
        throw new DurableApiError(409, "already_decided");
      // The real POST answers after the local attempt finalizes, so the usual state is terminal.
      const approval: FakeApproval = {
        ...input,
        native_request: { ...input.native_request },
        choice: { ...input.choice },
        state: "delivered_locally",
        reason: "",
        created_at: AT,
        updated_at: AT,
      };
      approvals.set(input.choice_id, approval);
      return approvalReceipt(approval);
    },
    async getApproval(choiceId) {
      const approval = approvals.get(choiceId);
      if (!approval) throw new DurableApiError(404, "not_found");
      return approvalReceipt(approval);
    },
    async getApprovalChoice(choiceId) {
      const approval = approvals.get(choiceId);
      if (!approval) throw new DurableApiError(404, "not_found");
      const record: ApprovalRecord = {
        ...approvalReceipt(approval),
        choice: { ...approval.choice },
        request: displayedRequest(approval),
      };
      return record;
    },
    async listApprovals(options) {
      const limit = options?.limit ?? 50;
      const rows = [...approvals.values()]
        .filter((approval) => approval.choice_id > (options?.after ?? ""))
        .sort((a, b) => a.choice_id.localeCompare(b.choice_id));
      const page = rows.slice(0, limit);
      return {
        approvals: page.map(approvalReceipt),
        next_after: rows.length > limit ? (page.at(-1)?.choice_id ?? null) : null,
      };
    },
  };
}
