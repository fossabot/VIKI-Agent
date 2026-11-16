import type { BotRow } from "../../clients/messaging/botsApi";
import type { OtherSession, SessionRow } from "../../protocol";
import { entrypointBadge } from "../../state/entrypoint";
import { type RoutinesSnapshot, routineAttention, whenLabel } from "../../state/routines";
import { presence, type SessionView } from "../../state/view";

export interface TeamLive {
  online: boolean;
  sessions: Readonly<Record<string, SessionView | undefined>>;
  others: readonly OtherSession[];
  routines: RoutinesSnapshot | null;
  checker: "running" | "listed" | "absent" | "unknown";
  sessionRows: readonly SessionRow[];
  nowMs: number;
}

export type BotState =
  | "needs_you"
  | "working"
  | "elsewhere"
  | "away"
  | "unavailable"
  | "not_started"
  | "unknown"
  | "idle";

export interface BotStatus {
  state: BotState;
  text: string;
}

export const NOT_STARTED: BotStatus = { state: "not_started", text: "Not started" };
export const CHECKING: BotStatus = { state: "unknown", text: "Checking…" };
export const STATUS_UNAVAILABLE: BotStatus = { state: "unknown", text: "Status unavailable" };
const IDLE: BotStatus = { state: "idle", text: "Idle" };

const unavailable = (text: string): BotStatus => ({ state: "unavailable", text });

function unavailableStatus(bot: BotRow): BotStatus | null {
  if (!bot.profile) return unavailable("Its profile is unavailable.");
  if (bot.profile.archived) return unavailable("Its profile is archived.");
  if (!bot.workspace) return unavailable("Its vault is unavailable.");
  if (bot.chat.archived) return unavailable("Its conversation is archived.");
  return null;
}

export function botStatus(
  bot: BotRow,
  live: TeamLive,
  activeWorkspaceId: string | null | undefined,
): BotStatus {
  const gone = unavailableStatus(bot);
  if (gone) return gone;
  if (bot.workspace_id !== activeWorkspaceId)
    return { state: "away", text: `In ${bot.workspace?.name ?? ""}` };
  if (!live.online) return STATUS_UNAVAILABLE;

  const snapshot = live.routines;
  const ready = snapshot?.status === "ready" && snapshot.error === null;
  const owned = ready ? snapshot.items.filter((r) => bot.routine_ids.includes(r.routine_id)) : [];
  const chat = live.sessions[bot.chat_session_id];
  const chatPresence = chat ? presence(chat) : "idle";

  if (chatPresence === "waiting")
    return { state: "needs_you", text: "Waiting for you in its conversation" };
  for (const item of owned) {
    const attention = routineAttention(item, live.sessions);
    if (attention?.kind === "waiting")
      return { state: "needs_you", text: `${item.name} is waiting for your OK` };
    if (attention?.kind === "blocked")
      return { state: "needs_you", text: `${item.name} was blocked` };
    if (attention?.kind === "expired")
      return { state: "needs_you", text: `${item.name} expired waiting for you` };
  }

  if (chatPresence === "thinking") return { state: "working", text: "Thinking" };
  if (chatPresence === "writing") return { state: "working", text: "Writing a reply" };
  const running = owned.find((r) => r.active_run);
  if (running) return { state: "working", text: `Running ${running.name}` };

  const other = live.others.find((o) => o.session_id === bot.chat_session_id);
  if (other) {
    const entrypoint =
      other.entrypoint ||
      live.sessionRows.find((r) => r.session_id === bot.chat_session_id)?.entrypoint ||
      "";
    const badge = entrypointBadge(entrypoint);
    return { state: "elsewhere", text: badge ? `Live elsewhere (${badge})` : "Live elsewhere" };
  }

  if (bot.routine_ids.length > 0) {
    if (!snapshot) return STATUS_UNAVAILABLE;
    if (!ready)
      return snapshot.error === null &&
        (snapshot.status === "idle" || snapshot.status === "loading")
        ? CHECKING
        : STATUS_UNAVAILABLE;
    if (owned.length < bot.routine_ids.length) return STATUS_UNAVAILABLE;
  }

  const next = owned
    .filter((r) => r.next_run !== null && whenLabel(r.next_run, live.nowMs) !== "")
    .sort((a, b) => Date.parse(a.next_run ?? "") - Date.parse(b.next_run ?? ""))[0];
  if (next?.next_run)
    return { state: "idle", text: `Next: ${next.name} ${whenLabel(next.next_run, live.nowMs)}` };
  return bot.chat.started || (chat?.turns.length ?? 0) > 0 ? IDLE : NOT_STARTED;
}

export function checkerStatus(live: TeamLive): BotStatus {
  if (!live.online) return STATUS_UNAVAILABLE;
  if (live.checker === "running") return { state: "working", text: "Checking a card" };
  if (live.checker === "listed") return { state: "idle", text: "Ready" };
  if (live.checker === "absent") return unavailable("Not in this vault");
  return STATUS_UNAVAILABLE;
}

export function statusCounts(statuses: readonly BotStatus[]): {
  needsYou: number;
  working: number;
} {
  return {
    needsYou: statuses.filter((s) => s.state === "needs_you").length,
    working: statuses.filter((s) => s.state === "working").length,
  };
}

export function countText(counts: { needsYou: number; working: number }): string {
  const parts: string[] = [];
  if (counts.needsYou > 0)
    parts.push(counts.needsYou === 1 ? "1 needs you" : `${counts.needsYou} need you`);
  if (counts.working > 0) parts.push(`${counts.working} working`);
  return parts.join(" · ");
}
