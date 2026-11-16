/**
 * The review shown before a failed or cancelled live turn is retried (Conversation's Retry…
 * action). It lists what the turn's own tools already did, flags the ones that may have changed
 * files or talked to the network, and says plainly that nothing here is undone by retrying.
 *
 * Its one action puts the turn's original prompt (and attachments) back in the message box, and
 * only when that box is empty — the user's own Send is what actually creates a new command.
 * Nothing in this component sends anything itself.
 */
import { type ReactElement, useRef } from "react";
import { WRITE_TOOLS } from "../../state/unload";
import { promptCut, rejoinPrompt, type Turn } from "../../state/view";
import type { Attachment } from "../../turnInput";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./RetryReview.css";

const writeTools = new Set(WRITE_TOOLS);
const WEB_TOOLS = new Set(["WebFetch", "WebSearch"]);

export interface RetryToolRow {
  tool: string;
  summary: string;
  flag: "write" | "mcp" | "web" | null;
}

function toolFlag(tool: string): RetryToolRow["flag"] {
  if (writeTools.has(tool)) return "write";
  if (tool.startsWith("mcp__")) return "mcp";
  if (WEB_TOOLS.has(tool)) return "web";
  return null;
}

/** Every tool row the turn ran, in arrival order: a "tools" block's own rows, then a "routing"
 * block's nested ones — the same rows the transcript itself shows (Blocks.tsx), flattened and
 * flagged for the review. */
export function retryFlags(turn: Turn): RetryToolRow[] {
  const rows: RetryToolRow[] = [];
  for (const block of turn.blocks) {
    if (block.kind === "tools") {
      for (const row of block.rows)
        rows.push({ tool: row.tool, summary: row.summary, flag: toolFlag(row.tool) });
    } else if (block.kind === "routing") {
      for (const row of block.tools)
        rows.push({ tool: row.tool, summary: row.summary, flag: toolFlag(row.tool) });
    }
  }
  return rows;
}

const FLAG_LABEL: Record<NonNullable<RetryToolRow["flag"]>, string> = {
  write: "write",
  mcp: "MCP",
  web: "Web",
};

/** The refusal shown when this window holds only a cut copy of a prompt (`promptCut`): App's
 * composer refusal for Edit in a new chat, and this component's own paragraph below Put the
 * prompt back in the message box. Built once so the two surfaces' copy never drifts apart, and to
 * match the archive export's own line naming both routes (`ConversationTools.tsx`). For a history
 * turn (`turn.history`) the pointer names both routes — Send history for a prompt this app sent
 * before a reload or restart, Open conversation record for any other earlier conversation — since
 * a history turn may or may not have a ledger row. Retry never opens on a history turn
 * (`Conversation.tsx`'s `canRetry`), so `forRetry` has no effect there: the history branch has no
 * Retry variant. When `user.at` is empty or unparsable, the sentence drops the "(sent …)" clause
 * instead of printing an invalid date. With `recordMissing` (the view's history read answered
 * `transcript_missing`) a history turn's pointer drops Open conversation record: the record it
 * opens is gone, so naming it would send the reader to a dead end. */
export function cutPromptNotice(turn: Turn, forRetry: boolean, recordMissing = false): string {
  const ms = Date.parse(turn.user?.at ?? "");
  const sent = Number.isNaN(ms) ? "" : new Date(ms).toLocaleString("en-US");
  const cut = `Only this window's copy of this prompt was cut short${sent ? ` (sent ${sent})` : ""}.`;
  if (turn.history && recordMissing) {
    return `${cut} Open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app.`;
  }
  if (turn.history) {
    return `${cut} Open its full text: Drafts and send history → Send history → Restore text for a prompt sent from this app, Open conversation record for an earlier conversation.`;
  }
  return `${cut} Drafts and send history → Send history → Restore text opens the full text in a new conversation's message box${forRetry ? "; copy it from there into this one to retry here." : "."}`;
}

export interface RetryReviewProps {
  turn: Turn;
  editorEmpty: boolean;
  /** True when this window holds only part of the session (`view.truncated`): the tool list
   * below may be missing rows, so it must never claim there were none. The notice renders above
   * the list. */
  incomplete: boolean;
  onFill: (text: string, attachments: readonly Attachment[]) => void;
  onClose: () => void;
}

export function RetryReview({
  turn,
  editorEmpty,
  incomplete,
  onFill,
  onClose,
}: RetryReviewProps): ReactElement {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useModalFocus({ active: true, dialogRef, initialFocusRef: closeRef, onDismiss: onClose });
  const rows = retryFlags(turn);
  const agents = turn.blocks.filter((block) => block.kind === "routing");
  const user = turn.user;
  const cut = promptCut(turn);

  function handleFill(): void {
    if (!editorEmpty || !user || cut) return;
    onFill(rejoinPrompt(user), user.attachments ?? []);
  }

  return (
    <div
      ref={dialogRef}
      className="retry-review"
      role="dialog"
      aria-modal="true"
      aria-labelledby="retry-review-title"
      tabIndex={-1}
    >
      <div id="retry-review-title">Before you retry</div>
      {incomplete ? (
        <p>
          This list may be incomplete: this window holds only part of the conversation. Open
          conversation record for the full record.
        </p>
      ) : null}
      {agents.length > 0 ? (
        <>
          <p>
            Agent work was observed in this turn. Individual tool calls may be missing from this
            window. File or network effects may already exist; retrying does not undo them.
          </p>
          {agents.map((agent) => (
            <p key={agent.id}>
              {agent.agent}
              {": "}
              {agent.toolUses === undefined
                ? "Tool count unavailable."
                : `Provider reported ${agent.toolUses} tool use${agent.toolUses === 1 ? "" : "s"}.`}
            </p>
          ))}
        </>
      ) : null}
      {rows.length === 0 ? (
        incomplete || agents.length > 0 ? null : (
          <p>No tools ran in this turn.</p>
        )
      ) : (
        <>
          <p>
            These tools ran in this turn. Write, MCP and web tools may already have changed files or
            sent data. Network effects are not undone.
          </p>
          <ul>
            {rows.map((row, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: rows are recomputed whole from the turn's own immutable blocks on every render; their order never changes for a given turn.
              <li key={index}>
                <span>{row.tool}</span>
                {row.flag ? <span>{FLAG_LABEL[row.flag]}</span> : null}
                <span>{row.summary}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <button type="button" onClick={handleFill} disabled={!editorEmpty || cut}>
        Put the prompt back in the message box
      </button>
      {cut ? (
        <p>{cutPromptNotice(turn, true)}</p>
      ) : !editorEmpty ? (
        <p>Only an empty message box can take it.</p>
      ) : null}
      <button type="button" ref={closeRef} onClick={onClose}>
        Close
      </button>
    </div>
  );
}
