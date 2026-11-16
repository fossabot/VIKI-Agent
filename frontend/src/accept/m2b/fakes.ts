/**
 * Builders for the m2b acceptance tests (frozen with them): session views assembled through
 * the real reducer, so the rows under test see exactly what the socket produces.
 */
import type { Envelope } from "../../protocol";
import { applyEnvelope, applyHistoryPage } from "../../state/sessionReducer";
import { emptyView, type SessionView } from "../../state/view";

export const SID = "s1";
export const AT = "2026-09-12T09:05:00.000Z";
export const USAGE = {
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_creation_tokens: 0,
};

export interface Item {
  type: string;
  data: Record<string, unknown>;
}

/** Folds envelopes (seq 1, 2, 3, ...) into a view. */
export function view(items: Item[], start: SessionView = emptyView(SID)): SessionView {
  return items
    .map(
      (item, i) =>
        ({ v: 1, session_id: SID, seq: start.seq + i + 1, ...item }) as unknown as Envelope,
    )
    .reduce(applyEnvelope, start);
}

/** The same items as one history page (turns marked history, no durations). */
export function historyView(items: Item[]): SessionView {
  return applyHistoryPage(emptyView(SID), {
    events: items,
    next_before_uuid: null,
  } as unknown as Parameters<typeof applyHistoryPage>[1]);
}

export function userMessage(text: string, command = "", at = AT): Item {
  return { type: "user.message", data: { text, command, at } };
}

export const turnStarted: Item = { type: "turn.started", data: { turn_id: "t1" } };

export function textDelta(text: string, blockId = "b1"): Item {
  return { type: "text.delta", data: { block_id: blockId, text } };
}

export function textDone(text: string, blockId = "b1"): Item {
  return { type: "text.done", data: { block_id: blockId, text } };
}

export function thinkingDelta(text: string, blockId = "k1"): Item {
  return { type: "thinking.delta", data: { block_id: blockId, text } };
}

export function thinkingDone(text: string, durationMs = 6000, blockId = "k1"): Item {
  return { type: "thinking.done", data: { block_id: blockId, text, duration_ms: durationMs } };
}

export function toolStarted(
  blockId: string,
  tool: string,
  row: { head: string; rest: string; tail?: string },
  parentId = "",
): Item {
  return {
    type: "tool.started",
    data: { block_id: blockId, tool, row: { tail: "", ...row }, parent_id: parentId },
  };
}

export function toolDone(
  blockId: string,
  options: { isError?: boolean; summary?: string; preview?: string; tail?: string } = {},
): Item {
  const { isError = false, summary = "ok", preview = "", tail = "" } = options;
  return {
    type: "tool.done",
    data: { block_id: blockId, is_error: isError, summary, preview, tail },
  };
}

export function subagentStarted(
  blockId: string,
  agent: string,
  model: string,
  reason = "",
  description = "",
): Item {
  return {
    type: "subagent.started",
    data: { block_id: blockId, agent, model, description, reason },
  };
}

export function subagentDone(blockId: string, status: string): Item {
  return { type: "subagent.done", data: { block_id: blockId, status } };
}

export function turnDone(status: "success" | "cancelled" | "error" = "success"): Item {
  return {
    type: "turn.done",
    data: { status, usage: USAGE, context_percent: null, duration_ms: 1200 },
  };
}
