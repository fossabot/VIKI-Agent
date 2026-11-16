/**
 * The messages the user sends to the model to have a decision applied: a vault-proposal
 * hand-off and a vault-proposal reject. Pure text builders. Most of the wording is the
 * `decisions.handoff.*` catalog rows; the user's own notes are carried as English-only
 * literals, never added to that catalog.
 */
import { tf } from "../../i18n/t";
import type { ProposalDetail, VaultProposalItem } from "../../state/decisionsApi";

/** A fence one backtick longer than the longest run in the text, never under three: the same rule
 * as the server's `render_excerpt_block`, so the text cannot close its own block. */
export function fenceFor(text: string): string {
  const longest = Math.max(0, ...(text.match(/`+/g) ?? []).map((run) => run.length));
  return "`".repeat(Math.max(longest + 1, 3));
}

/** A note edit is a change to a note that exists. Where the server sent no diff (too large, or the
 * note is gone) the message carries the whole proposed text instead of an empty block. A create-only
 * proposal (no base revision) asks for a new note and always carries the whole text. */
export function proposalHandoffText(detail: ProposalDetail): string {
  const proposed = detail.revision.slice(0, 12);
  const createOnly = detail.base_revision === null;
  const hasDiff = !createOnly && detail.diff !== "";
  const body = hasDiff ? detail.diff : detail.proposed_text;
  const fence = fenceFor(body);
  const sentence = createOnly
    ? tf("decisions.handoff.proposal_create", { ref: detail.ref, proposed })
    : tf(hasDiff ? "decisions.handoff.proposal_apply" : "decisions.handoff.proposal_replace", {
        ref: detail.ref,
        base: (detail.base_revision ?? "").slice(0, 12),
        proposed,
      });
  return [sentence, "", `${fence}${hasDiff ? "diff" : ""}`, body, fence].join("\n");
}

/** The origin noun for a withdrawn vault proposal. English-only (program rule), so it is a
 * literal here rather than a `decisions.handoff.*` catalog row. */
function rejectOrigin(originKind: string): string {
  if (originKind === "note_edit") return "note edit proposal";
  if (originKind === "memory_promotion") return "memory proposal";
  return `${originKind} proposal`;
}

/** Withdrawing a vault proposal drafts this note for VIKI. `note` is `""` when the user
 * gave none. */
export function proposalRejectText(
  item: Pick<VaultProposalItem, "ref" | "origin_kind">,
  note: string,
): string {
  const origin = rejectOrigin(item.origin_kind);
  const sentence = note
    ? `Reject the ${origin} for ${item.ref}: ${note}`
    : `Reject the ${origin} for ${item.ref}.`;
  const reasonClause = note ? " and its reason" : "";
  return [
    sentence,
    "",
    `The app withdrew it, so the note is unchanged. Record this rejection${reasonClause} where /retro will see it.`,
  ].join("\n");
}
