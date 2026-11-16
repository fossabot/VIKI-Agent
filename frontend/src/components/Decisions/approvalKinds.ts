/**
 * The approval action registry: given one decision item, which of approve, edit and reject it
 * offers, in that order, each with its label, hint and optional note kind. Pure — no React, no
 * I/O. It covers the vault proposal, the `skill_proposal` item kind and two more.
 * `question_proposal`: approve while open, reject relabelled Undo once it is in use (approved),
 * never edit or later. `change_proposal`: approve while open or
 * approved (approved relabels it Draft again and answers the same draft again, with no write),
 * reject only while open; never edit or later, since the app never writes the vault itself.
 */
import type { NativeDecision } from "../../state/decisions";
import type {
  ChangeProposalItem,
  ChangeProposalState,
  QuestionProposalItem,
  QuestionProposalState,
  VaultProposalItem,
  VaultProposalState,
} from "../../state/decisionsApi";
import type { SkillProposalItem, SkillState } from "../../state/skillProposalsApi";

export type ApprovalAction = "approve" | "edit" | "reject" | "later";

export interface ApprovalActionSpec {
  action: ApprovalAction;
  label: string;
  hint: string;
  note: "reason" | "edit" | null;
  enabled: boolean;
}

export const NOTE_MAX = 200;

const OTHER_VAULT_HINT = "This proposal is for another vault.";
const WRONG_STATE_HINT = "Not available in this state.";

// Mirrors ProposalInspector's HANDOFF_STATES: the states offering approve.
const HANDOFF_STATES: ReadonlySet<VaultProposalState> = new Set(["open", "handed"]);
// Mirrors SkillProposalActions' own APPROVABLE: the states offering approve.
const SKILL_APPROVABLE: ReadonlySet<SkillState> = new Set(["open", "handed"]);
// A question proposal's reject action (labelled Undo once in use): open can still be rejected
// outright; approved ("in use") can only be undone back to the wording before it.
const QUESTION_UNDOABLE: ReadonlySet<QuestionProposalState> = new Set(["open", "approved"]);
// A change proposal's own act rule (`changes.py::_ACTS`): approve
// fits open or approved (approved writes nothing and answers the same draft again); reject fits
// open only. A rejected row offers neither.
const CHANGE_APPROVABLE: ReadonlySet<ChangeProposalState> = new Set(["open", "approved"]);

// A question proposal's own four words: its "open" means
// "Being tried", not the generic "Proposed" a vault or skill proposal's "open" reads. Exported so
// Approvals (DecisionSection) and the right rail's Needs you row (NeedsYou) share this one source
// of truth instead of each typing the word.
export const QUESTION_STATUS_LABELS: Readonly<Record<QuestionProposalState, string>> = {
  open: "Being tried",
  approved: "In use",
  replaced: "Replaced",
  rejected: "Rejected",
};

/** A disabled spec's hint: "for another vault" when not active, else "wrong state". */
function disabledHint(active: boolean): string {
  return active ? WRONG_STATE_HINT : OTHER_VAULT_HINT;
}

// Row 1: a native, inline tool permission. Row 2 (a plan or a question) offers nothing.
function nativeActions(item: NativeDecision): readonly ApprovalActionSpec[] {
  if (item.kind !== "permission" || !item.inline) return [];
  return [
    {
      action: "approve",
      label: "Allow once",
      hint: "Allows this one request",
      note: null,
      enabled: true,
    },
    { action: "reject", label: "Deny", hint: "Denies this request", note: null, enabled: true },
  ];
}

// Row 5: a vault proposal. Edit and reject are gated only by `active`; approve also needs a
// hand-off-eligible state, so it is the only one of the three that the wrong-state hint ever reaches.
function vaultActions(item: VaultProposalItem, active: boolean): readonly ApprovalActionSpec[] {
  const canApprove = active && HANDOFF_STATES.has(item.state);
  return [
    {
      action: "approve",
      label: "Approve",
      hint: canApprove ? "Asks VIKI, in a new draft, to apply this change." : disabledHint(active),
      note: null,
      enabled: canApprove,
    },
    {
      action: "edit",
      label: "Edit",
      hint: active ? "Opens the proposal to edit the note." : disabledHint(active),
      note: null,
      enabled: active,
    },
    {
      action: "reject",
      label: "Reject",
      hint: active ? "Withdraws the proposal and drafts a note for VIKI." : disabledHint(active),
      note: "reason",
      enabled: active,
    },
  ];
}

// Row 6: a skill proposal. Mirrors SkillProposalActions' own rules: approve, edit and reject each
// gated by their own state set, all three disabled together (the other-vault hint) when inactive.
// Unlike a vault proposal, edit and reject are not gated by `active` alone: SkillProposalActions
// rules each of them out in a few more states too, so they get the same wrong-state hint approve
// already needed. Never offers later.
function skillActions(item: SkillProposalItem, active: boolean): readonly ApprovalActionSpec[] {
  const canApprove = active && SKILL_APPROVABLE.has(item.state);
  const canEdit = active && item.state !== "applied";
  const canReject = active && item.state !== "applied" && item.state !== "rejected";
  return [
    {
      action: "approve",
      label: "Approve",
      hint: canApprove ? "Opens a draft asking VIKI to apply this skill" : disabledHint(active),
      note: null,
      enabled: canApprove,
    },
    {
      action: "edit",
      label: "Edit",
      hint: canEdit ? "Opens the proposal to edit the skill." : disabledHint(active),
      note: null,
      enabled: canEdit,
    },
    {
      action: "reject",
      label: "Reject",
      hint: canReject
        ? "Records a rejection; an optional reason stays with the proposal."
        : disabledHint(active),
      note: "reason",
      enabled: canReject,
    },
  ];
}

// Row 7: a question-wording proposal. Approve only while open; reject sends the same action
// whether it reads "Reject" (open — nothing is in use yet) or "Undo" (approved — goes back to
// the wording before it). A replaced or rejected proposal is closed to both. Never offers edit
// or later.
function questionActions(
  item: QuestionProposalItem,
  active: boolean,
): readonly ApprovalActionSpec[] {
  const canApprove = active && item.state === "open";
  const canUndo = active && QUESTION_UNDOABLE.has(item.state);
  const inUse = item.state === "approved";
  return [
    {
      action: "approve",
      label: "Approve",
      hint: canApprove ? "Starts using this wording for new decisions." : disabledHint(active),
      note: null,
      enabled: canApprove,
    },
    {
      action: "reject",
      label: inUse ? "Undo" : "Reject",
      hint: canUndo
        ? inUse
          ? "Goes back to the wording before this change."
          : "Rejects this wording; nothing changes."
        : disabledHint(active),
      note: "reason",
      enabled: canUndo,
    },
  ];
}

// Row 8: a vault-change proposal. Approve opens a
// draft asking VIKI to make the change; once approved it relabels to Draft again and answers the
// same draft once more, with no further write. Reject (optional reason) fits only the open row:
// nothing in the vault changes either way. Never offers edit or later.
function changeActions(item: ChangeProposalItem, active: boolean): readonly ApprovalActionSpec[] {
  const canApprove = active && CHANGE_APPROVABLE.has(item.state);
  const canReject = active && item.state === "open";
  return [
    {
      action: "approve",
      label: item.state === "approved" ? "Draft again" : "Approve",
      hint: canApprove ? "Opens a draft asking VIKI to make this change." : disabledHint(active),
      note: null,
      enabled: canApprove,
    },
    {
      action: "reject",
      label: "Reject",
      hint: canReject ? "Rejects this change; nothing in the vault changes." : disabledHint(active),
      note: "reason",
      enabled: canReject,
    },
  ];
}

/** Order is approve, edit, reject. Active means `origin.workspace_id ===
 * ctx.activeWorkspaceId`. */
export function approvalActions(
  item:
    | NativeDecision
    | VaultProposalItem
    | SkillProposalItem
    | QuestionProposalItem
    | ChangeProposalItem,
  ctx: { activeWorkspaceId: string | null },
): readonly ApprovalActionSpec[] {
  if (item.kind === "vault_proposal") {
    return vaultActions(item, item.origin.workspace_id === ctx.activeWorkspaceId);
  }
  if (item.kind === "skill_proposal") {
    return skillActions(item, item.origin.workspace_id === ctx.activeWorkspaceId);
  }
  if (item.kind === "question_proposal") {
    return questionActions(item, item.origin.workspace_id === ctx.activeWorkspaceId);
  }
  if (item.kind === "change_proposal") {
    return changeActions(item, item.origin.workspace_id === ctx.activeWorkspaceId);
  }
  return nativeActions(item);
}
