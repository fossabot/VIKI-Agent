/**
 * The right rail's "Needs you" merge: native permission/question cards and vault proposal rows
 * the user must still act on, deduplicated into one ordered list. Pure and
 * React-free, like `collectDecisions` and `groupRuns` it draws from.
 *
 * Rules:
 * - Natives come from `collectDecisions`, in its order. A permission card naming an agent joins
 *   the one `groupRuns` `needs` row of the same session whose `requests` list its `tool_use_id`:
 *   agent and blockId then come from that row's block. `groupRuns` is called with this function's
 *   own optional `requests` input, so two same-named running rows can still be told apart by
 *   run id rather than only by the agent-name fallback. Without such a match — including when the
 *   card names no agent, or is a question — agent falls back to the card's own `data.agent` (or
 *   null) and blockId stays null.
 * - Proposal rows are the proposals with `attention`, in route order, minus a vault, skill,
 *   question or change proposal whose `origin.workspace_id` is not this window's active
 *   `workspaceId` — another vault's proposal still exists, but this window cannot act on it, so
 *   it neither counts nor shows here (it still lists, disabled, in Approvals).
 * - Order: natives, then proposals.
 */

import { collectDecisions, type NativeDecision } from "./decisions";
import type { ChangeProposalItem, QuestionProposalItem, VaultProposalItem } from "./decisionsApi";
import { groupRuns, type RunRequests } from "./runGroups";
import type { SkillProposalItem } from "./skillProposalsApi";
import type { SessionView } from "./view";

export type ProposalNeed =
  | VaultProposalItem
  | SkillProposalItem
  | QuestionProposalItem
  | ChangeProposalItem;

export type NeedRow =
  | {
      kind: "native";
      key: string;
      item: NativeDecision;
      agent: string | null;
      blockId: string | null;
    }
  | { kind: "proposal"; key: string; item: ProposalNeed };

export interface NeedsYou {
  rows: NeedRow[];
  count: number;
}

export function needsYou(input: {
  sessions: Readonly<Record<string, SessionView>>;
  workspaceId: string | null;
  proposals: readonly ProposalNeed[] | null /* null: unread */;
  requests?: RunRequests;
}): NeedsYou {
  const rows: NeedRow[] = [];
  const proposals: readonly ProposalNeed[] = input.proposals ?? [];

  const groups = groupRuns(input.sessions, new Set(), input.requests);
  for (const item of collectDecisions(input.sessions, input.workspaceId)) {
    let agent: string | null = null;
    let blockId: string | null = null;
    if (item.card.kind === "permission" && item.card.data.agent !== "") {
      const toolUseId = item.card.data.tool_use_id;
      const owner = groups.needs.find(
        (row) =>
          row.sessionId === item.origin.session_id &&
          row.requests.some((request) => request.tool_use_id === toolUseId),
      );
      if (owner) {
        agent = owner.block.agent;
        blockId = owner.block.id;
      } else {
        agent = item.card.data.agent;
      }
    }
    rows.push({ kind: "native", key: `native:${item.id}`, item, agent, blockId });
  }

  for (const item of proposals) {
    if (!item.attention) continue;
    if (item.origin.workspace_id !== input.workspaceId) continue;
    rows.push({ kind: "proposal", key: `proposal:${item.kind}:${item.id}`, item });
  }

  return {
    rows,
    count: rows.length,
  };
}
