/**
 * The right rail's run groups: every subagent run across the open chats, grouped by what it
 * needs from the user right now. Pure and React-free — `AgentRuns` (components/RightRail)
 * renders what this module computes; `stats` in `runCounts` is whatever `useRunStats`
 * (runStats.ts) last read for that run, kept as a small structural type here so this module
 * stays independent of that one.
 *
 * Rules:
 * - `runKey(sessionId, blockId)` = `${sessionId}:${blockId}` identifies one routing block across
 *   a reload; `reviewed` (passed into `groupRuns`) and a caller's own "mark reviewed" trade in
 *   this same key.
 * - `groupRuns`'s optional `requests: RunRequests` (`useRunRequests`'s state, keyed by session
 *   then by `tool_use_id`) names each open request's run id and arrival time as a `RequestInfo`
 *   (`Pick<OpenRequest, "run" | "raisedAt">` — src/clients/conversations/runsApi.ts's `OpenRequest` minus the id it is
 *   already keyed by there). Leaving it out, or leaving a given card's id out of it, falls back
 *   to the pre-`requests` behaviour everywhere below.
 * - `groupRuns` walks `Object.entries(sessions)`, each session's turns, each turn's blocks, in
 *   order. Every `routing` block becomes a `RunRow`: `background` is true exactly when the block
 *   is still "running" while its own turn is not (the same rule the chat's own routing row uses
 *   for "Running in background" — Conversation.tsx's `block.status === "running" &&
 *   !turnRunning`), forced back to `false` on an `ended` row regardless; `live` is true unless
 *   the row's turn was rebuilt from history; `ended` is true once the session has closed while
 *   the block itself is still "running" — its CLI process is gone, so it will never report a
 *   real end.
 * - Attribution: an open `kind: "permission"` card whose `data.agent` is non-empty first checks
 *   `requests` for that card's own `tool_use_id`. When that names a `run` id that is a running
 *   row of the same session, the card's data joins that row's `requests` and attribution stops
 *   there. Otherwise the type rule applies, unchanged: matched against that same session's own
 *   running rows with `block.agent === data.agent`. Exactly one match and the card's data joins
 *   that row's `requests`; zero matches or several and it joins the session's one `SessionNeed`
 *   instead — an agent *type* cannot tell two same-type runs apart (their `run` id can), and the
 *   generic "subagent" fallback never names a real run either. A question card, and a card with
 *   the main thread's `""` agent, are never attributed anywhere, `requests` or not.
 * - `waitingSince` (on a `RunRow` and on a `SessionNeed`) is the smallest `raisedAt` that
 *   `requests` reports for any one of its own `requests` cards, by `tool_use_id`; `null` when
 *   none of them are known to `requests` — including whenever `groupRuns` is called without it.
 * - Grouping: an `ended` row always goes to `done` (background forced `false`), ahead of every
 *   other rule. Otherwise: a running row with any `requests` → `needs`; running with none →
 *   `working`; a completed or failed row that is still `live` and not yet in `reviewed` →
 *   `review`; everything else (stopped rows, history rows, and finished rows already reviewed) →
 *   `done`. `needs` and `working` keep walk order; `review` and `done` are walk order reversed:
 *   newest first within each chat, the chats in the reverse of the order this window first added
 *   them (the store keeps an existing chat's place) — a `RunRow` carries no timestamp of its
 *   own, so arrival order is the only clock.
 * - `runCounts`, `runLine` and `runElapsed` word a row for display; see each one's own comment.
 */

import type { OpenRequest } from "../clients/conversations/runsApi";
import type { PermissionRequestedData } from "../protocol";
import type { RoutingBlock, SessionView } from "./view";

/** One open request's run attribution and arrival time, keyed by its `tool_use_id` within
 * `RunRequests`. */
export type RequestInfo = Pick<OpenRequest, "run" | "raisedAt">;

/** A window's open requests: by session id, then by `tool_use_id`. What `useRunRequests` holds
 * and `groupRuns`'s optional third argument reads. */
export type RunRequests = ReadonlyMap<string, ReadonlyMap<string, RequestInfo>>;

export interface RunRow {
  key: string;
  sessionId: string;
  turnId: string;
  block: RoutingBlock;
  background: boolean;
  live: boolean;
  /** The session closed while this block was still "running": it will never finish on its own. */
  ended: boolean;
  /** The smallest known `raisedAt` among `requests`, else `null`. See the module doc. */
  waitingSince: number | null;
  requests: PermissionRequestedData[];
}

export interface SessionNeed {
  sessionId: string;
  requests: PermissionRequestedData[];
  /** The smallest known `raisedAt` among `requests`, else `null`. See the module doc. */
  waitingSince: number | null;
}

export interface RunGroups {
  needs: RunRow[];
  working: RunRow[];
  review: RunRow[];
  done: RunRow[];
  unattributed: SessionNeed[];
}

export function runKey(sessionId: string, blockId: string): string {
  return `${sessionId}:${blockId}`;
}

export function groupRuns(
  sessions: Readonly<Record<string, SessionView>>,
  reviewed: ReadonlySet<string>,
  requests?: RunRequests,
): RunGroups {
  const needs: RunRow[] = [];
  const working: RunRow[] = [];
  const review: RunRow[] = [];
  const done: RunRow[] = [];
  const unattributed: SessionNeed[] = [];

  for (const [sessionId, view] of Object.entries(sessions)) {
    const sessionRequests = requests?.get(sessionId);
    const rows: RunRow[] = [];
    for (const turn of view.turns) {
      for (const block of turn.blocks) {
        if (block.kind !== "routing") continue;
        rows.push({
          key: runKey(sessionId, block.id),
          sessionId,
          turnId: turn.id,
          block,
          background: block.status === "running" && turn.status !== "running",
          live: !turn.history,
          ended: view.closed && block.status === "running",
          waitingSince: null,
          requests: [],
        });
      }
    }

    const chatLevel: PermissionRequestedData[] = [];
    for (const card of view.cards) {
      if (card.kind !== "permission") continue;
      const agent = card.data.agent;
      if (agent === "") continue;
      const namedRun = sessionRequests?.get(card.data.tool_use_id)?.run ?? null;
      const namedRow =
        namedRun !== null
          ? rows.find((row) => row.block.status === "running" && row.block.id === namedRun)
          : undefined;
      if (namedRow) {
        namedRow.requests.push(card.data);
        continue;
      }
      const owners = rows.filter(
        (row) => row.block.status === "running" && row.block.agent === agent,
      );
      if (owners.length === 1) owners.at(0)?.requests.push(card.data);
      else chatLevel.push(card.data);
    }
    if (chatLevel.length > 0) {
      unattributed.push({
        sessionId,
        requests: chatLevel,
        waitingSince: earliestRaisedAt(sessionRequests, chatLevel),
      });
    }

    for (const row of rows) {
      row.waitingSince = earliestRaisedAt(sessionRequests, row.requests);
      if (row.ended) {
        row.background = false;
        done.push(row);
      } else if (row.block.status === "running") {
        (row.requests.length > 0 ? needs : working).push(row);
      } else if (
        (row.block.status === "completed" || row.block.status === "failed") &&
        row.live &&
        !reviewed.has(row.key)
      ) {
        review.push(row);
      } else {
        done.push(row);
      }
    }
  }

  review.reverse();
  done.reverse();

  return { needs, working, review, done, unattributed };
}

/** The smallest `raisedAt` that `sessionRequests` reports for any of `cards`, by `tool_use_id`;
 * `null` when none are in it (including whenever it is `undefined`, i.e. `groupRuns` was not
 * given a `requests` map). */
function earliestRaisedAt(
  sessionRequests: ReadonlyMap<string, RequestInfo> | undefined,
  cards: readonly PermissionRequestedData[],
): number | null {
  let earliest: number | null = null;
  for (const card of cards) {
    const raisedAt = sessionRequests?.get(card.tool_use_id)?.raisedAt;
    if (raisedAt === undefined) continue;
    if (earliest === null || raisedAt < earliest) earliest = raisedAt;
  }
  return earliest;
}

function toolUseCount(n: number): string {
  return `${n} tool use${n === 1 ? "" : "s"}`;
}

function tokenCount(n: number): string {
  return n < 1000 ? `${n} tokens` : `${(n / 1000).toFixed(1)}k tokens`;
}

/** `ms` as a short duration for a list row ticking once a second: seconds alone under a minute,
 * minutes and seconds under an hour, else hours and minutes (RoutingRow's own `.routing-meta`
 * keeps its separate tenths-precision `elapsed()` for the chat view; this one is whole seconds). */
export function runElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (s < 3600) return `${m}m ${s % 60}s`;
  const h = Math.floor(s / 3600);
  return `${h}h ${m % 60}m`;
}

/** The row's counts line (joined " · "). Running (and not `ended`): `waiting` + elapsed time
 * since `waitingSince` first, once known, then elapsed time from `stats.startedAt` once known,
 * nested tool uses once any tool row has arrived, `~` + `stats.tokens` once known, then "in
 * background" when the run outlived its own turn. Finished, and an `ended` row too: the status
 * word (Done, Failed or Stopped — an `ended` row always reads Stopped), then the CLI's own
 * `toolUses` with `totalTokens` and `durationMs` where present, or else the nested tool count
 * alone (an `ended` row's own status is still "running", so it never has CLI counts and always
 * falls to the nested count). `stats` is `undefined` when no `SubagentsApi` is wired. */
export function runCounts(
  row: RunRow,
  stats: { startedAt: number | null; tokens: number | null } | undefined,
  nowMs: number,
): string {
  const status = row.block.status;
  const parts: string[] = [];
  if (status === "running" && !row.ended) {
    if (row.waitingSince !== null) {
      parts.push(`waiting ${runElapsed(Math.max(0, nowMs - row.waitingSince))}`);
    }
    if (stats !== undefined && stats.startedAt !== null) {
      parts.push(runElapsed(Math.max(0, nowMs - stats.startedAt)));
    }
    if (row.block.tools.length > 0) parts.push(toolUseCount(row.block.tools.length));
    if (stats !== undefined && stats.tokens !== null) parts.push(`~${tokenCount(stats.tokens)}`);
    if (row.background) parts.push("in background");
  } else {
    parts.push(
      row.ended
        ? "Stopped"
        : status === "completed"
          ? "Done"
          : status === "failed"
            ? "Failed"
            : "Stopped",
    );
    if (row.block.toolUses !== undefined) {
      parts.push(toolUseCount(row.block.toolUses));
      if (row.block.totalTokens !== undefined) parts.push(tokenCount(row.block.totalTokens));
      if (row.block.durationMs !== undefined) parts.push(runElapsed(row.block.durationMs));
    } else if (row.block.tools.length > 0) {
      parts.push(toolUseCount(row.block.tools.length));
    }
  }
  return parts.join(" · ");
}

/** The row's one-line "doing now" (also its `title`). An `ended` row always shows its own closing
 * `summary` (or "" without one) and nothing else — its pending requests, if any, are moot once
 * the session has closed. Otherwise: a row with pending requests names the first one's tool and
 * summary, plus how many more are waiting; a running row with none shows its newest nested tool
 * row's head and rest (one space between them when there is a rest, the head alone otherwise —
 * this line is flat text, with no CSS gap to supply that space the way `ToolBlockRows` does),
 * trailing " …" while that tool is not done yet; a finished row shows the CLI's own closing
 * summary. Empty when none of those have anything yet. */
export function runLine(row: RunRow): string {
  if (row.ended) return row.block.summary ?? "";
  const first = row.requests.at(0);
  if (first) {
    const more = row.requests.length - 1;
    const suffix = more > 0 ? ` (+${more} more)` : "";
    return `Waiting for approval · ${first.tool} ${first.summary}${suffix}`;
  }
  if (row.block.status === "running") {
    const newest = row.block.tools.at(-1);
    if (!newest) return "";
    const text = newest.row.rest ? `${newest.row.head} ${newest.row.rest}` : newest.row.head;
    return newest.done ? text : `${text} …`;
  }
  return row.block.summary ?? "";
}
