/** Pure generation-scoped event reduction. Snapshots reconstruct live state separately from
 * transcript history; native request identities prevent stale answers from closing new cards.
 */
import type {
  Envelope,
  HistoryItem,
  HistoryPage,
  NativeRequest,
  SessionSnapshot,
  UserMessageData,
} from "../protocol";
import type {
  Block,
  Card,
  NoticeBlock,
  RoutingBlock,
  SessionView,
  ToolBlock,
  ToolRowView,
  Turn,
} from "./view";
import { emptyView } from "./view";

/** Projection metadata lives beside type/data; the frozen SnapshotItem does not declare it. */
type BoundedSnapshotItem = SessionSnapshot["data"]["events"][number] & {
  original_bytes?: number;
  source_seq?: unknown;
};

/** The history reader cuts a prompt's text to this many code points (`mapper.py`'s
 * `_history_user`, `text[: self._text_limit]`, with the limit `stream_state.MAX_TEXT_CHARS`) and
 * says nothing when it does. A prompt of exactly this length is treated as cut even if it was
 * whole: a refused reuse is safe, a silently shortened one is not. */
const HISTORY_TEXT_LIMIT = 16_384;

export function applyEnvelope(view: SessionView, envelope: Envelope): SessionView {
  return reduceEnvelope(view, envelope, envelope.seq);
}

function reduceEnvelope(
  view: SessionView,
  envelope: Envelope,
  completionSeq: number | undefined,
): SessionView {
  if (
    envelope.session_id !== view.sessionId ||
    (view.generation !== null && envelope.generation !== view.generation) ||
    (view.requestId !== null && envelope.request_id !== view.requestId) ||
    envelope.seq <= view.seq
  )
    return view;
  const seq = envelope.seq;
  const base = { ...view, seq, generation: envelope.generation, requestId: envelope.request_id };
  switch (envelope.type) {
    case "session.started":
      return {
        ...base,
        info: envelope.data,
        mode: envelope.data.mode,
        blocked: null,
        closed: false,
        error: null,
      };
    case "session.blocked":
      return { ...base, blocked: envelope.data };
    case "session.closed":
      return { ...base, closed: true, cards: [], steers: [] };
    case "user.message": {
      const last = view.turns.at(-1);
      if (last && last.status === "running" && !last.history) {
        // Claude Code folded this message into the turn it was already running instead of
        // opening a new one for it: close that turn here, before any turn.started of its own
        // ever would, and open the continuation as `<id>:steer` — turn.done later finds and
        // closes it the same way it closes any other turn, by taking the last one.
        const steered: Turn = { ...last, status: "success", steered: true };
        return {
          ...base,
          error: null,
          rateLimit: null,
          turns: [
            ...replaceTurn(view.turns, view.turns.length - 1, steered),
            newTurn(`${last.id}:steer`, envelope.data),
          ],
        };
      }
      return {
        ...base,
        error: null,
        rateLimit: null,
        turns: [...view.turns, newTurn(`local-${seq}`, envelope.data)],
      };
    }
    case "turn.started": {
      const last = view.turns.at(-1);
      if (last && last.status === "running" && last.id.startsWith("local-")) {
        // Only a rename: this turn's user.message already dropped the old limit, and the frame
        // that would re-announce it arrives later in the turn than system/init does.
        const renamed = { ...last, id: envelope.data.turn_id };
        return { ...base, turns: replaceTurn(view.turns, view.turns.length - 1, renamed) };
      }
      // A turn the CLI started by itself opens one just as much as a send does, so it drops the
      // old limit too — otherwise a session the user only watches never clears the card.
      return {
        ...base,
        rateLimit: null,
        turns: [...view.turns, newTurn(envelope.data.turn_id, null)],
      };
    }
    // A message not yet read by Claude Code: appended (replacing any earlier one with the same
    // id) until its steer.ended arrives, live or from a snapshot's own waiting-steers bucket —
    // this case and the next run the same way either path (see SteerPendingData's own comment).
    case "steer.pending":
      return {
        ...base,
        steers: [
          ...(view.steers ?? []).filter((s) => s.steer_id !== envelope.data.steer_id),
          envelope.data,
        ],
      };
    case "steer.ended":
      return {
        ...base,
        steers: (view.steers ?? []).filter((s) => s.steer_id !== envelope.data.steer_id),
        // "dropped" means it never reached Claude Code; say so where the user is looking.
        turns:
          envelope.data.outcome === "dropped"
            ? pushBlock(
                view.turns,
                notice(
                  `steer-${envelope.data.steer_id}`,
                  "error",
                  "not_sent",
                  "This message didn't reach Claude Code. Find its text in Drafts and send history.",
                ),
              )
            : view.turns,
      };
    case "question.asked":
      return withCard(base, {
        kind: "question",
        data: envelope.data,
        nativeRequest: envelope.native_request ?? null,
      });
    case "permission.requested":
      return withCard(base, {
        kind: "permission",
        data: envelope.data,
        nativeRequest: envelope.native_request ?? null,
      });
    case "request.answered":
    case "request.cancelled":
      return {
        ...base,
        cards: view.cards.filter(
          (card) =>
            card.data.tool_use_id !== envelope.data.tool_use_id ||
            !sameRequest(card.nativeRequest, envelope.native_request ?? null),
        ),
      };
    case "tool.done":
      return {
        ...base,
        cards: withoutCard(view.cards, envelope.data.block_id),
        turns: applyToTurns(view.turns, envelope, String(seq), false),
      };
    case "tool.started":
      return { ...base, turns: applyToTurns(view.turns, envelope, String(seq), false, seq) };
    case "subagent.done":
      return {
        ...base,
        turns: applyToTurns(view.turns, envelope, String(seq), false, undefined, completionSeq),
      };
    case "status":
      return {
        ...base,
        mode: envelope.data.mode || view.mode,
        compacting: envelope.data.state === "compacting",
      };
    case "compacted":
      return {
        ...base,
        compacting: false,
        turns: applyToTurns(view.turns, envelope, String(seq), false),
      };
    case "rate_limit":
      return { ...base, rateLimit: envelope.data };
    case "vault.sessions":
      return base;
    case "turn.done":
      return {
        ...base,
        // A subagent's card outlives the turn: its run may still be going in the background, and
        // the CLI ends turns of its own while one does (scenario 28). The chat's own cards end here.
        cards: view.cards.filter((card) => card.kind === "permission" && card.data.agent !== ""),
        contextPercent: envelope.data.context_percent ?? view.contextPercent,
        turns: applyToTurns(view.turns, envelope, String(seq), false),
      };
    case "error": {
      if (view.turns.at(-1)?.status === "running") {
        return { ...base, turns: applyToTurns(view.turns, envelope, String(seq), false) };
      }
      return { ...base, error: envelope.data };
    }
    default:
      return { ...base, turns: applyToTurns(view.turns, envelope, String(seq), false) };
  }
}

/** A snapshot reconstructs live turns and open requests; history conversion would close them. */
export function applySnapshot(before: SessionView, snapshot: SessionSnapshot): SessionView {
  const sameGeneration = before.generation === snapshot.generation;
  let view: SessionView = {
    ...emptyView(snapshot.session_id),
    generation: snapshot.generation,
    requestId: snapshot.request_id,
  };
  for (const [index, item] of snapshot.data.events.entries()) {
    const sourceSeq = (item as BoundedSnapshotItem).source_seq;
    view = reduceEnvelope(
      view,
      {
        ...item,
        v: 2,
        session_id: snapshot.session_id,
        generation: snapshot.generation,
        request_id: snapshot.request_id,
        seq: index + 1,
      } as Envelope,
      typeof sourceSeq === "number" &&
        Number.isSafeInteger(sourceSeq) &&
        sourceSeq > 0 &&
        sourceSeq <= snapshot.seq
        ? sourceSeq
        : undefined,
    );
    if (
      item.type === "user.message" &&
      (item as BoundedSnapshotItem).original_bytes !== undefined
    ) {
      const last = view.turns.at(-1);
      if (last)
        view = {
          ...view,
          turns: replaceTurn(view.turns, view.turns.length - 1, { ...last, userCut: true }),
        };
    }
  }
  const state = snapshot.data.state;
  return {
    ...view,
    seq: snapshot.seq,
    info: state.info,
    mode: state.mode,
    blocked: state.blocked,
    closed: state.closed,
    error: state.error,
    compacting: state.compacting,
    contextPercent: state.context_percent,
    rateLimit: state.rate_limit,
    truncated: snapshot.data.truncated,
    turns: mergeHistory(sameGeneration ? before.turns : [], view.turns),
    historyCursor: sameGeneration || before.generation === null ? before.historyCursor : undefined,
    historyLoading: sameGeneration && before.historyLoading,
    historyRequestId: null,
    historyError: sameGeneration ? before.historyError : null,
  };
}

export function sameRequest(a: NativeRequest | null, b: NativeRequest | null): boolean {
  return a === null || b === null
    ? a === b
    : a.runtime_id === b.runtime_id && a.request_id === b.request_id && a.turn_id === b.turn_id;
}

/**
 * Prepend an older history page: its items become turns (history: true, status "success"),
 * a new turn at every user.message (items before the first one form a turn with user null).
 * Turn ids are `h-<next_before_uuid or "start">-<index>`. historyCursor = next_before_uuid;
 * historyLoading = false.
 */
export function applyHistoryPage(view: SessionView, page: HistoryPage["data"]): SessionView {
  if (page.error)
    return {
      ...view,
      historyLoading: false,
      historyRequestId: null,
      // busy keeps the cursor so a later reload (or the older-pill, on a later page) can resume
      // paging; every other error is terminal for this view.
      historyCursor: page.error === "busy" ? view.historyCursor : null,
      historyError: page.error,
    };
  const prefix = `h-${page.next_before_uuid ?? "start"}`;
  let oldestSkillCallOrder = 0;
  for (const turn of view.turns) {
    for (const block of turn.blocks) {
      if (block.kind !== "tools" && block.kind !== "routing") continue;
      const rows = block.kind === "tools" ? block.rows : block.tools;
      for (const row of rows) {
        if (row.tool === "Skill" && typeof row.skillCallOrder === "number")
          oldestSkillCallOrder = Math.min(oldestSkillCallOrder, row.skillCallOrder);
      }
    }
  }
  const pageBase = oldestSkillCallOrder - page.events.length;
  let turns: Turn[] = [];
  for (const [index, item] of page.events.entries()) {
    if (item.type === "user.message") {
      const turn = newTurn(`${prefix}-${turns.length}`, item.data);
      const cut = [...item.data.text].length === HISTORY_TEXT_LIMIT;
      turns = [...turns, cut ? { ...turn, userCut: true } : turn];
      continue;
    }
    if (turns.length === 0) turns = [newTurn(`${prefix}-0`, null)];
    turns = applyToTurns(turns, item, `h${index}`, true, pageBase + index);
  }
  const finished = turns.map(
    (turn): Turn => ({
      ...turn,
      history: true,
      status: turn.status === "running" ? "success" : turn.status,
      // A routing block the page never closed would otherwise stay "running" forever, and
      // runningAgents() counts it: opening an old conversation made the rails say a subagent is
      // "here now" days after it ran. It happens on ordinary transcripts — a background agent's
      // row closes only on a later task_notification, which may have landed in a newer page (the
      // reader builds a fresh Mapper per page) or never been written. "stopped" is what the page
      // actually recorded: no result. Some of those agents did finish, so the word is a guess
      // about how they ended — but never a guess that they are still running.
      blocks: turn.blocks.map((block) =>
        block.kind === "routing" && block.status === "running"
          ? { ...block, status: "stopped" as const }
          : block,
      ),
    }),
  );
  return {
    ...view,
    turns: mergeHistory(finished, view.turns),
    historyCursor: page.next_before_uuid,
    historyLoading: false,
    historyRequestId: null,
    // A partial page's tail window ends the chain when next_before_uuid is null: not "no older
    // history" but the budget cutting it off, so the UI must say why. A partial page
    // with a cursor still has more to page through and applies like a complete one.
    historyError:
      page.partial === "tail_budget" && page.next_before_uuid === null ? "tail_budget" : null,
  };
}

/** Local card deletion helper. V2 actions wait for authoritative request events instead. */
export function answerCard(view: SessionView, toolUseId: string): SessionView {
  return { ...view, cards: withoutCard(view.cards, toolUseId) };
}

// --- turns and blocks ---------------------------------------------------------------------------

function newTurn(id: string, user: UserMessageData | null): Turn {
  return {
    id,
    user: user
      ? {
          text: user.text,
          command: user.command,
          at: user.at,
          ...(user.attachments?.length
            ? { attachments: user.attachments.map((item) => ({ ...item })) }
            : {}),
          ...(user.unavailable_attachments?.length
            ? { unavailable_attachments: user.unavailable_attachments.map((item) => ({ ...item })) }
            : {}),
        }
      : null,
    blocks: [],
    status: "running",
    usage: null,
    durationMs: 0,
    history: false,
  };
}

function withCard(view: SessionView, card: Card): SessionView {
  if (
    view.cards.some(
      (c) =>
        c.data.tool_use_id === card.data.tool_use_id &&
        sameRequest(c.nativeRequest, card.nativeRequest),
    )
  )
    return view;
  return { ...view, cards: [...withoutCard(view.cards, card.data.tool_use_id), card] };
}

function withoutCard(cards: Card[], toolUseId: string): Card[] {
  return cards.filter((c) => c.data.tool_use_id !== toolUseId);
}

function replaceTurn(turns: readonly Turn[], index: number, turn: Turn): Turn[] {
  const next = turns.slice();
  next[index] = turn;
  return next;
}

/** Keep older history without duplicating current runs; only missing tool rows carry forward. */
function mergeHistory(older: Turn[], current: Turn[]): Turn[] {
  const runs = new Map<string, RoutingBlock>();
  for (const turn of current) {
    for (const block of turn.blocks) {
      if (block.kind === "routing") runs.set(block.id, block);
    }
  }
  const retained: Turn[] = [];
  for (const turn of older) {
    const blocks = turn.blocks.filter((block) => {
      if (block.kind !== "routing") return true;
      const run = runs.get(block.id);
      if (!run) return true;
      const ids = new Set(run.tools.map((row) => row.id));
      const tools = block.tools.filter((row) => !ids.has(row.id));
      if (tools.length > 0) {
        const merged = { ...run, tools: [...tools, ...run.tools] };
        runs.set(block.id, merged);
        current = updateBlock(current, block.id, () => merged) ?? current;
      }
      return false;
    });
    if (turn.history)
      retained.push(blocks.length === turn.blocks.length ? turn : { ...turn, blocks });
  }
  return [...retained, ...current];
}

/** Block-level events on the last turn; lookups by id reach earlier turns. Unchanged turns
 * come back as the same array. */
function applyToTurns(
  turns: Turn[],
  item: HistoryItem,
  label: string,
  history: boolean,
  skillCallOrder?: number,
  completionSeq?: number,
): Turn[] {
  switch (item.type) {
    case "text.delta":
      return streamed(turns, "text", item.data.block_id, item.data.text, false, null);
    case "text.done":
      return streamed(turns, "text", item.data.block_id, item.data.text, true, null);
    case "thinking.delta":
      return streamed(turns, "thinking", item.data.block_id, item.data.text, false, null);
    case "thinking.done":
      return streamed(
        turns,
        "thinking",
        item.data.block_id,
        item.data.text,
        true,
        history ? null : item.data.duration_ms,
      );
    case "tool.started": {
      const row: ToolRowView = {
        id: item.data.block_id,
        tool: item.data.tool,
        row: item.data.row,
        ...(item.data.tool === "Skill" && skillCallOrder !== undefined ? { skillCallOrder } : {}),
        done: false,
        isError: false,
        summary: "",
        preview: "",
        artifactStart: item.data.artifact,
      };
      if (item.data.parent_id) {
        const nested = updateBlock(turns, item.data.parent_id, (block) =>
          block.kind === "routing" ? { ...block, tools: [...block.tools, row] } : null,
        );
        if (nested) return nested;
      }
      const last = turns.at(-1);
      if (!last) return turns;
      const tail = last.blocks.at(-1);
      if (tail?.kind === "tools") {
        const grown: ToolBlock = { ...tail, rows: [...tail.rows, row] };
        const blocks = last.blocks.slice(0, -1);
        blocks.push(grown);
        return replaceTurn(turns, turns.length - 1, { ...last, blocks });
      }
      return pushBlock(turns, { kind: "tools", id: row.id, rows: [row] });
    }
    case "tool.done": {
      const { block_id, is_error, summary, preview, tail } = item.data;
      return (
        updateRow(turns, block_id, (row) => ({
          ...row,
          done: true,
          isError: is_error,
          summary,
          preview,
          row: tail ? { ...row.row, tail } : row.row,
          artifactResult: item.data.artifact ?? row.artifactResult,
        })) ?? turns
      );
    }
    case "subagent.started": {
      const started: RoutingBlock = {
        kind: "routing",
        id: item.data.block_id,
        agent: item.data.agent,
        model: item.data.model,
        description: item.data.description,
        reason: item.data.reason,
        status: "running",
        tools: [],
      };
      const current = history ? turns : turns.filter((turn) => !turn.history);
      let updated =
        updateBlock(current, started.id, (block) =>
          block.kind === "routing" ? { ...started, tools: block.tools } : null,
        ) ?? pushBlock(current, started);
      if (history) return updated;
      if (updated.length === 0)
        updated = [{ ...newTurn(`subagent-${label}`, null), status: "success", blocks: [started] }];
      return mergeHistory(
        turns.filter((turn) => turn.history),
        updated,
      );
    }
    case "subagent.done":
      return (
        updateBlock(turns, item.data.block_id, (block) =>
          block.kind === "routing"
            ? {
                ...block,
                status: item.data.status,
                toolUses: item.data.tool_uses,
                totalTokens: item.data.total_tokens,
                durationMs: item.data.duration_ms,
                summary: item.data.summary,
                completionSeq,
              }
            : null,
        ) ?? turns
      );
    case "permission.denied":
      return pushBlock(
        turns,
        notice(`permission.denied-${label}`, "denied", item.data.tool, item.data.reason),
      );
    case "hook":
      return pushBlock(turns, notice(`hook-${label}`, "hook", item.data.name, item.data.message));
    case "compacted":
      return pushBlock(turns, notice(`compacted-${label}`, "compacted", item.data.trigger, ""));
    case "error":
      return pushBlock(turns, notice(`error-${label}`, "error", item.data.code, item.data.message));
    case "turn.done": {
      const last = turns.at(-1);
      if (!last) return turns;
      const ended: Turn = {
        ...last,
        status: item.data.status,
        usage: item.data.usage,
        durationMs: item.data.duration_ms,
      };
      return replaceTurn(turns, turns.length - 1, ended);
    }
    default:
      return turns;
  }
}

function notice(
  id: string,
  tone: NoticeBlock["tone"],
  subject: string,
  detail: string,
): NoticeBlock {
  return { kind: "notice", id, tone, subject, detail };
}

function streamed(
  turns: Turn[],
  kind: "text" | "thinking",
  id: string,
  text: string,
  done: boolean,
  durationMs: number | null,
): Turn[] {
  const updated = updateBlock(turns, id, (block) => {
    if (block.kind !== kind) return null;
    if (block.done && !done) return block;
    if (!done) return { ...block, text: block.text + text };
    return block.kind === "thinking"
      ? { ...block, text, done: true, durationMs }
      : { ...block, text, done: true };
  });
  if (updated) return updated;
  const block: Block =
    kind === "text"
      ? { kind: "text", id, text, done }
      : { kind: "thinking", id, text, done, durationMs: done ? durationMs : null };
  return pushBlock(turns, block);
}

/** Append a block to the last turn; dropped when there is no turn. */
function pushBlock(turns: Turn[], block: Block): Turn[] {
  const last = turns.at(-1);
  if (!last) return turns;
  return replaceTurn(turns, turns.length - 1, { ...last, blocks: [...last.blocks, block] });
}

/** Replace the block with `id`, searching the last turn first. Null when there is none, or when
 * `change` declines the block it found (a kind mismatch): the caller then falls back as if the
 * id had never been seen. The same `turns` when `change` returns the block unchanged. */
function updateBlock(
  turns: Turn[],
  id: string,
  change: (block: Block) => Block | null,
): Turn[] | null {
  for (let t = turns.length - 1; t >= 0; t--) {
    const turn = turns[t];
    if (!turn) continue;
    const b = turn.blocks.findIndex((block) => block.id === id);
    if (b < 0) continue;
    const block = turn.blocks[b];
    if (!block) continue;
    const changed = change(block);
    if (!changed) return null;
    if (changed === block) return turns;
    const blocks = turn.blocks.slice();
    blocks[b] = changed;
    return replaceTurn(turns, t, { ...turn, blocks });
  }
  return null;
}

/** Replace the tool row with `id` (in a ToolBlock or under a RoutingBlock), the last turn
 * first; null when there is none. */
function updateRow(
  turns: Turn[],
  id: string,
  change: (row: ToolRowView) => ToolRowView,
): Turn[] | null {
  for (let t = turns.length - 1; t >= 0; t--) {
    const turn = turns[t];
    if (!turn) continue;
    for (let b = 0; b < turn.blocks.length; b++) {
      const block = turn.blocks[b];
      if (!block || (block.kind !== "tools" && block.kind !== "routing")) continue;
      const rows = block.kind === "tools" ? block.rows : block.tools;
      const r = rows.findIndex((row) => row.id === id);
      if (r < 0) continue;
      const row = rows[r];
      if (!row) continue;
      const changed = rows.slice();
      changed[r] = change(row);
      const replaced: ToolBlock | RoutingBlock =
        block.kind === "tools" ? { ...block, rows: changed } : { ...block, tools: changed };
      const blocks = turn.blocks.slice();
      blocks[b] = replaced;
      return replaceTurn(turns, t, { ...turn, blocks });
    }
  }
  return null;
}
