/**
 * The decision inbox's items: every open card, in every tracked session, derived from the view
 * snapshot. Pure state — no React, no I/O — so the inbox and the card column can share one
 * source of what is still open and what this window knows about it.
 */
import type { VaultProposalItem } from "./decisionsApi";
import type { Card, SessionView } from "./view";

const RESOURCE_LIMIT = 200;

/** A native permission or question card, reachable from any tracked session. */
export interface NativeDecision {
  kind: "permission" | "question";
  id: string;
  origin: { workspace_id: string | null; session_id: string; turn_id: string };
  revision: string;
  provider: "Claude Code";
  actor: string;
  action: string;
  resource: string;
  inline: boolean;
  card: Card;
}

/** What the inbox holds: native cards derived from the view snapshot, plus the vault proposals
 * the decisions route returns. `collectDecisions` yields only the native kind. */
export type DecisionItem = NativeDecision | VaultProposalItem;

/** Cuts to at most 200 code points without splitting a surrogate pair. */
function cutResource(value: string): string {
  const codePoints = Array.from(value);
  return codePoints.length > RESOURCE_LIMIT ? codePoints.slice(0, RESOURCE_LIMIT).join("") : value;
}

/** Walks every open, tracked session's cards in object then arrival order, deriving one item per
 * card that carries a native request. Closed requests never reach here: the reducer removes
 * their cards. */
export function collectDecisions(
  sessions: Readonly<Record<string, SessionView>>,
  workspaceId: string | null,
): NativeDecision[] {
  const items: NativeDecision[] = [];
  for (const [sessionId, view] of Object.entries(sessions)) {
    if (view.closed || !view.generation) continue;
    for (const card of view.cards) {
      const nativeRequest = card.nativeRequest;
      if (!nativeRequest) continue;
      const actor = card.kind === "permission" ? card.data.agent || "VIKI" : "VIKI";
      const action = card.kind === "permission" ? card.data.tool : "AskUserQuestion";
      const resource =
        card.kind === "permission" ? card.data.summary : (card.data.questions[0]?.question ?? "");
      const inline = card.kind === "permission" && card.data.kind === "tool";
      items.push({
        kind: card.kind,
        id: `${sessionId}:${card.data.tool_use_id}`,
        origin: {
          workspace_id: workspaceId,
          session_id: sessionId,
          turn_id: nativeRequest.turn_id,
        },
        revision: nativeRequest.request_id,
        provider: "Claude Code",
        actor,
        action,
        resource: cutResource(resource),
        inline,
        card,
      });
    }
  }
  return items;
}
