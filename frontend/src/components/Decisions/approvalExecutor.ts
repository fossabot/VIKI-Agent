/**
 * Runs one approval action against the server: a vault proposal's hand-off or withdraw, a skill
 * proposal's approve or reject, a question proposal's approve, reject or undo, or a change
 * proposal's approve or reject. The vault's hand-off and withdraw, the skill's approve, and the
 * change's approve (open or, relabelled Draft again, approved) each open their own draft for the
 * user to send after; the skill's reject and every question- or change-proposal reject open
 * none. A pure factory — no React.
 */
import type { DecisionLoopApi } from "../../clients/decisionLoop/types";
import {
  type DecisionsApi,
  isChangeProposalItem,
  isQuestionProposalItem,
} from "../../state/decisionsApi";
import type { ProposalNeed } from "../../state/needsYou";
import { type SkillProposalsApi, skillRefusal } from "../../state/skillProposalsApi";
import type { ApprovalAction } from "./approvalKinds";
import { proposalHandoffText, proposalRejectText } from "./handoff";
import { applyHandoffText } from "./skillHandoff";

/** Thrown when an approval action cannot proceed; `ownerText` is shown to the user verbatim. */
export class ApprovalActionError extends Error {
  constructor(
    readonly code: "changed" | "editor_full" | "other_vault" | "not_supported" | "refused",
    readonly ownerText: string,
  ) {
    super(code);
  }
}

export function createApprovalExecutor(d: {
  api: DecisionsApi;
  skills?: SkillProposalsApi;
  questions?: Pick<DecisionLoopApi, "actProposal" | "actChange">;
  activeWorkspaceId: string | null;
  openDraft(text: string): boolean;
  changed(): void;
}): (row: ProposalNeed, action: ApprovalAction, note: string) => Promise<void> {
  return async (row, action, note) => {
    try {
      if (row.origin.workspace_id !== d.activeWorkspaceId) {
        throw new ApprovalActionError("other_vault", "This proposal is for another vault.");
      }
      if (row.kind === "skill_proposal") {
        if (!d.skills) {
          throw new ApprovalActionError("not_supported", "This action isn't available from here.");
        }
        const skills = d.skills;
        if (action === "approve") {
          const detail = await skills.detail(row.id);
          if (detail.revision !== row.revision) {
            throw new ApprovalActionError(
              "changed",
              "This proposal changed since you opened it. Review it again before approving.",
            );
          }
          try {
            await skills.act(row.id, "approve", detail.revision);
          } catch (error) {
            throw new ApprovalActionError("refused", skillRefusal(error));
          }
          if (!d.openDraft(applyHandoffText(detail))) {
            throw new ApprovalActionError(
              "editor_full",
              "The approval was recorded, but this window has no room for another draft. Close a draft, then use Approve in the proposal to make the draft again.",
            );
          }
        } else if (action === "reject") {
          try {
            await skills.act(row.id, "reject", row.revision, undefined, note || undefined);
          } catch (error) {
            throw new ApprovalActionError("refused", skillRefusal(error));
          }
        } else {
          throw new ApprovalActionError("not_supported", "This action isn't available from here.");
        }
        return;
      }
      if (isQuestionProposalItem(row)) {
        if (!d.questions) {
          throw new ApprovalActionError("not_supported", "This action isn't available from here.");
        }
        if (action !== "approve" && action !== "reject") {
          throw new ApprovalActionError("not_supported", "This action isn't available from here.");
        }
        // Approve and reject (shown as Undo once the wording is in use) both apply right away,
        // server-side compare-and-set on the row's own revision: no draft either way.
        await d.questions.actProposal(row.id, action, row.revision, note || undefined);
        return;
      }
      if (isChangeProposalItem(row)) {
        if (!d.questions) {
          throw new ApprovalActionError("not_supported", "This action isn't available from here.");
        }
        if (action !== "approve" && action !== "reject") {
          throw new ApprovalActionError("not_supported", "This action isn't available from here.");
        }
        // Compare-and-set on the row's own revision, as a question proposal's act is. Reject
        // answers no draft; approve answers one both while open and, relabelled Draft again,
        // once approved (the server writes nothing the second time, only answers it again).
        const result = await d.questions.actChange(row.id, action, row.revision, note || undefined);
        if (action === "reject") return;
        if (result.draft === null || !d.openDraft(result.draft)) {
          throw new ApprovalActionError(
            "editor_full",
            "The approval was recorded, but this window has no room for another draft. Close a draft, then use Draft again in Approvals.",
          );
        }
        return;
      }
      if (action === "approve") {
        const detail = await d.api.proposalDetail(row.id);
        if (detail.revision !== row.revision) {
          throw new ApprovalActionError(
            "changed",
            "This proposal changed since you opened it. Review it again before approving.",
          );
        }
        await d.api.proposalAct(row.id, "handoff");
        if (!d.openDraft(proposalHandoffText(detail))) {
          throw new ApprovalActionError(
            "editor_full",
            "The hand-off was recorded, but this window has no room for another draft. Close a draft, then use Create a draft in the proposal.",
          );
        }
      } else if (action === "reject") {
        await d.api.proposalAct(row.id, "withdraw");
        if (!d.openDraft(proposalRejectText(row, note))) {
          throw new ApprovalActionError(
            "editor_full",
            "The proposal was withdrawn, but this window has no room for the draft to VIKI.",
          );
        }
      } else {
        throw new ApprovalActionError("not_supported", "This action isn't available from here.");
      }
    } finally {
      d.changed();
    }
  };
}
