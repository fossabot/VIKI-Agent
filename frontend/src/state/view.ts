/**
 * The view model: what the reducer builds from protocol events and what components render.
 * Components read this projection; generation-scoped recovery updates it through the store.
 */
import type {
  ArtifactResult,
  ArtifactStart,
  ErrorData,
  NativeRequest,
  PermissionRequestedData,
  QuestionAskedData,
  RateLimitData,
  SessionBlockedData,
  SessionStartedData,
  SteerPendingData,
  ToolRow,
  TurnStatus,
  Usage,
} from "../protocol";

export interface TextBlock {
  kind: "text";
  id: string;
  text: string;
  done: boolean;
}

export interface ThinkingBlock {
  kind: "thinking";
  id: string;
  text: string;
  done: boolean;
  /** null while streaming and on history rows (transcripts keep no duration). */
  durationMs: number | null;
}

export interface ToolRowView {
  id: string;
  tool: string;
  row: ToolRow;
  /** Local Skill start order from live envelope sequence or an older history page. */
  skillCallOrder?: number;
  done: boolean;
  isError: boolean;
  summary: string;
  preview: string;
  artifactStart?: ArtifactStart | undefined;
  artifactResult?: ArtifactResult | undefined;
}

/** Consecutive main-thread tool calls share one block (a ToolBlock in the design). */
export interface ToolBlock {
  kind: "tools";
  id: string; // the first row's id
  rows: ToolRowView[];
}

/** A subagent (RoutingRow); its own tool calls nest in `tools`. */
export interface RoutingBlock {
  kind: "routing";
  id: string;
  agent: string;
  model: string;
  description: string;
  reason: string;
  status: "running" | "completed" | "failed" | "stopped";
  tools: ToolRowView[];
  toolUses?: number | undefined;
  totalTokens?: number | undefined;
  durationMs?: number | undefined;
  summary?: string | undefined;
  /** Original terminal envelope sequence; absent when snapshot/history provenance is unknown. */
  completionSeq?: number | undefined;
}

/** One-line rows inside a turn; components word them. denied: subject = tool, detail = reason ·
 * hook: subject = hook name, detail = message · compacted: subject = trigger, detail = "" ·
 * error: subject = error code, detail = message. */
export interface NoticeBlock {
  kind: "notice";
  id: string;
  tone: "denied" | "hook" | "compacted" | "error";
  subject: string;
  detail: string;
}

export type Block = TextBlock | ThinkingBlock | ToolBlock | RoutingBlock | NoticeBlock;

export interface Turn {
  /** turn.started's turn_id; for history turns and before turn.started, a local id. */
  id: string;
  user: import("../protocol").UserMessageData | null;
  blocks: Block[];
  status: "running" | TurnStatus;
  usage: Usage | null;
  durationMs: number;
  /** Built from a history page (no durations, hooks or usage). */
  history: boolean;
  /** True when this window holds only a cut copy of the turn's prompt: a `session.snapshot` item
   * the server cut at its bound (`src/viki_agent/conversations/stream_state.py`'s `original_bytes`, absent from the frozen
   * protocol type), or a history prompt at the reader's cut length (sessionReducer.ts's
   * HISTORY_TEXT_LIMIT). Retry, ArrowUp recall, Edit in a new chat and the archive export all
   * refuse a cut prompt instead of passing it off as whole; quoting a turn (Quote into draft,
   * New chat with this turn) copies the window's text as shown, cut or not, but drops any
   * slash command. */
  userCut?: boolean;
  /** Set once a later message folded into this turn before it finished on its own
   * (sessionReducer.ts's user.message case): the user sent while Claude Code was still on this
   * turn's step, so it closed early instead of getting a turn.started of its own. The
   * turn-completion announcer (Conversation.tsx) skips this flip. */
  steered?: true;
}

/** Rejoins a turn's user message the way the live path split it (`session.py`'s `_command`):
 * a known slash command first, then its argument text, with no space when there is none. Retry,
 * ArrowUp recall, Edit in a new chat and the archive export all go through here before reusing a
 * prompt, so none of them drops the command; quoting a turn (Quote into draft, New chat with
 * this turn) does not go through here, and copies the window's text as shown without the
 * slash command. */
export function rejoinPrompt(user: NonNullable<Turn["user"]>): string {
  if (!user.command) return user.text;
  return user.text ? `${user.command} ${user.text}` : user.command;
}

/** True when this window holds only a cut copy of the turn's prompt: Retry, ArrowUp recall, Edit
 * in a new chat and the archive export all check this before reusing a prompt, so none of them
 * passes it off as whole; quoting a turn (Quote into draft, New chat with this turn) does not
 * check it, and copies the window's text as shown, without the slash command. */
export function promptCut(turn: Turn): boolean {
  return turn.userCut === true;
}

/** The wire payload nested whole: a permission card keeps its own `kind` ("tool" | "plan"),
 * which decides between a PermissionCard ("tool") and a DecisionCard ("plan"). */
export type Card =
  | { kind: "question"; data: QuestionAskedData; nativeRequest: NativeRequest | null }
  | { kind: "permission"; data: PermissionRequestedData; nativeRequest: NativeRequest | null };

export interface SessionView {
  sessionId: string;
  generation: string | null;
  requestId: string | null;
  synced: boolean;
  /** A correlated subscription refused before readiness was established. */
  openRefused?: "unavailable" | "storage_unavailable" | "model_review_required" | null;
  truncated: boolean;
  /** Last applied sequence within this generation and subscription. */
  seq: number;
  info: SessionStartedData | null;
  mode: string;
  turns: Turn[];
  /** Open cards in arrival order, keyed by tool_use_id. */
  cards: Card[];
  /** Messages Claude Code has not read yet, in arrival order, keyed by steer_id: steer.pending
   * until its steer.ended removes it (sessionReducer.ts). Optional like `openRefused` above, not
   * absent-never: a hand-built SessionView fixture that predates this field
   * (and so never sets it) must still satisfy this type; emptyView always sets it to `[]`. */
  steers?: SteerPendingData[];
  blocked: SessionBlockedData | null;
  closed: boolean;
  /** The latest error that is not part of a running turn (start failures, crashes). */
  error: ErrorData | null;
  compacting: boolean;
  contextPercent: number | null;
  rateLimit: RateLimitData | null;
  /** History paging: undefined = never loaded, null = no older page, else the next cursor. */
  historyCursor: string | null | undefined;
  /** A history.more is in flight: no second one is sent until its page arrives. */
  historyLoading: boolean;
  historyRequestId: string | null;
  /** Why older history stopped: the socket's history.page error, or "tail_budget" once a
   * partial page of an over-budget transcript reaches its window's start. */
  historyError: "too_large" | "unavailable" | "busy" | "transcript_missing" | "tail_budget" | null;
}

export type Presence = "idle" | "writing" | "thinking" | "waiting";

export function emptyView(sessionId: string): SessionView {
  return {
    sessionId,
    generation: null,
    requestId: null,
    synced: false,
    truncated: false,
    seq: 0,
    info: null,
    mode: "",
    turns: [],
    cards: [],
    steers: [],
    blocked: null,
    closed: false,
    error: null,
    compacting: false,
    contextPercent: null,
    rateLimit: null,
    historyCursor: undefined,
    historyLoading: false,
    historyRequestId: null,
    historyError: null,
  };
}

/** What the presence dot and header say: waiting for the user beats everything else. */
export function presence(view: SessionView): Presence {
  if (view.cards.length > 0) return "waiting";
  const turn = view.turns.at(-1);
  if (turn?.status !== "running") return "idle";
  const last = turn.blocks.at(-1);
  return last?.kind === "thinking" && !last.done ? "thinking" : "writing";
}
