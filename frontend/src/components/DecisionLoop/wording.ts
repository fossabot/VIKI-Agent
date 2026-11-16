/**
 * Decision loop word maps and the two time-wording helpers: the
 * kind chip text, the backend's fallback action labels, the feedback reason words, the
 * candidate/topic labels, a short relative-time phrase and the launcher panel's status line.
 * The Decision page's rows also share `localClock`, `RULES_ONLY_MODEL_STATES`, `formatNumber`,
 * `formatMoney`, `DAILY_CAP_OPTIONS`/`DailyCapOption`, `SHADOW_OPTIONS`, `readShadowSetting` and
 * `StatusLineView` from here.
 * Every string here is English only; `ItemCard.tsx` draws its own text from these, and the
 * panel and the reach/doing rows draw `statusLine`/`CANDIDATE_LABELS`/
 * `candidateLabel` from here too — this is the one place that owns the candidate→label lookup.
 */
import type {
  ActionType,
  DecisionKind,
  FeedbackReason,
  LauncherView,
  ModelState,
  ShadowSetting,
} from "../../clients/decisionLoop/types";
import type { ChoiceOption } from "./DecisionRow";

/** The item card's kind chip. */
export const KIND_LABELS: Readonly<Record<DecisionKind, string>> = {
  inform: "For your information",
  ask: "Question",
  suggest: "Suggestion",
  ready: "Ready for you",
  report: "Report",
};

/** Fallback text for an action whose server-sent `label` is empty, matching the backend's own
 * default wording for each action type one-for-one. The server's `label` is used first; this is
 * only the safety net so a button is never shown blank. */
export const ACTION_LABELS: Readonly<Record<ActionType, string>> = {
  open_chat: "Open chat",
  open_note: "Open note",
  open_approvals: "Review approvals",
  open_inbox: "Open inbox",
  open_routine: "Open routine",
  open_runs: "Open runs",
  open_context: "See why",
  open_vault: "Open vault",
  open_messaging: "Open Messaging",
  open_code: "Open in Code",
  open_triage: "Triage inbox",
  start_prepare: "Prepare a plan",
  start_run: "Start a chat to do it",
  stop_mission: "Stop",
  start_seat: "Start",
  stop_seat: "Stop",
  run_checks: "Run checks",
  stop_checks: "Stop",
  suggest_inbox: "Suggest destinations",
  draft_chat: "Draft it in this chat",
  draft_new: "Draft it in a new chat",
};

/** The item card's "⋯" menu: the four reasons behind "Not useful". */
export const REASON_LABELS: Readonly<Record<FeedbackReason, string>> = {
  out_of_date: "Out of date",
  already_knew: "Already knew",
  wrong_topic: "Wrong topic",
  bad_timing: "Bad timing",
};

/** Display label for a candidate (the Decision page's muted-topics and watch-switch rows);
 * `unknown` is the fallback for a topic value outside this known set, e.g. `"unwatched"` or a
 * future candidate this build does not list yet. */
export const CANDIDATE_LABELS: Readonly<Record<string, string>> & Readonly<{ unknown: string }> = {
  approvals: "Approvals",
  tasks_due: "Tasks due",
  agent_runs: "Chats and runs",
  inbox: "Inbox",
  uncommitted: "Uncommitted changes",
  routine: "Routines",
  mission_report: "Chats VIKI started",
  card_needs_you: "Cards that need you",
  card_stale: "Cards that stopped moving",
  seat_waiting: "Bot runs waiting for you",
  seat_working: "Bots working on cards",
  seat_start: "Bots that could start",
  seat_report: "Reports on Bot runs",
  vault_recovery: "Recovery in the vault",
  memory_drift: "Memories whose sources changed",
  bridge_stopped: "Stopped messaging bridges",
  chat_waiting: "Chats waiting for you",
  send_uncertain: "Messages that may not have arrived",
  mcp_context: "MCP servers filling context",
  code_look: "Code changes left uncommitted",
  long_task_look: "Second look at a long task",
  correction_look: "Second look after a correction",
  stalled_project: "Stalled project",
  overdue_tasks: "Overdue tasks to check",
  context_handoff: "Handoff for a full chat",
  source_note: "Notes for fetched pages",
  commit_checks: "Checks on a new commit",
  inbox_triage: "Inbox suggestions",
  skill_offer: "Skill offers",
  skill_suggest: "Skill suggestions",
  learn_source: "Sources to learn",
  retro_due: "Retro suggestions",
  question_tune: "Question tuning",
  note_link: "Note links",
  note_superseded: "Superseded notes",
  tasks_ticked: "Ticked tasks",
  unknown: "Other",
};

/** Display label for a candidate id; any id outside the known set (e.g. the wire's
 * `"unwatched"`, or a future candidate this build does not list yet) reads as
 * `CANDIDATE_LABELS.unknown` ("Other"). */
export function candidateLabel(candidate: string): string {
  return CANDIDATE_LABELS[candidate] ?? CANDIDATE_LABELS.unknown;
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** A stamp (ISO text) or an epoch-ms number to epoch ms, or `NaN` for text that does not parse. */
function toEpochMs(stampOrMs: string | number): number {
  return typeof stampOrMs === "number" ? stampOrMs : Date.parse(stampOrMs);
}

/** "just now" / "N min ago" / "1 hour ago" or "N hours ago" / "yesterday" / a short "M/D" date,
 * by elapsed time from `stampOrMs` to `nowMs`. A malformed stamp gives "". A stamp after `nowMs` (clock skew)
 * reads as "just now" rather than a negative count. */
export function relativeTime(stampOrMs: string | number, nowMs: number): string {
  const at = toEpochMs(stampOrMs);
  if (Number.isNaN(at)) return "";
  const diff = Math.max(0, nowMs - at);
  if (diff < MINUTE_MS) return "just now";
  if (diff < HOUR_MS) return `${Math.floor(diff / MINUTE_MS)} min ago`;
  if (diff < DAY_MS) {
    const hours = Math.floor(diff / HOUR_MS);
    return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
  }
  if (diff < 2 * DAY_MS) return "yesterday";
  const date = new Date(at);
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** "HH:MM" in local time, or "" for a stamp that does not parse. */
export function localClock(stamp: string): string {
  const at = new Date(stamp);
  if (Number.isNaN(at.getTime())) return "";
  return `${pad2(at.getHours())}:${pad2(at.getMinutes())}`;
}

export const RULES_ONLY_MODEL_STATES: ReadonlySet<ModelState> = new Set([
  "rules",
  "no_key",
  "key_rejected",
  "capped",
  "unreachable",
]);

/** `n` with thousands separators, pinned to en-US so a test's locale never moves the digits. */
export function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

/** Dollars to two places, or "< $0.01" below a cent. */
export function formatMoney(amountUsd: number): string {
  return amountUsd >= 0.01 ? `$${amountUsd.toFixed(2)}` : "< $0.01";
}

/** The daily spending caps (USD) the Decision model's cap select offers. */
export const DAILY_CAP_OPTIONS = [0.1, 0.25, 0.5, 1, 2] as const;
export type DailyCapOption = (typeof DAILY_CAP_OPTIONS)[number];

const SHADOW_SETTINGS: readonly ShadowSetting[] = ["off", "pinned", "latest", "preview"];

/** `decision.shadow`'s own default (contracts.py's `DecisionSettings.shadow: str = "off"`). */
export function readShadowSetting(values: Readonly<Record<string, unknown>>): ShadowSetting {
  const raw = values["decision.shadow"];
  return typeof raw === "string" && (SHADOW_SETTINGS as readonly string[]).includes(raw)
    ? (raw as ShadowSetting)
    : "off";
}

export const SHADOW_OPTIONS: readonly ChoiceOption<ShadowSetting>[] = [
  { value: "off", label: "Off" },
  { value: "pinned", label: "Pinned" },
  { value: "latest", label: "Latest stable" },
  { value: "preview", label: "Preview" },
];

/** The part of the launcher view `statusLine` reads, so the Decision page's card can pass the
 * page's own fields. */
export type StatusLineView = Pick<
  LauncherView,
  "state" | "paused_until" | "focus_until" | "checked_at" | "open" | "plans_waiting" | "model"
>;

/** The launcher panel's header status line: "Watching · N open · checked ..." while on, with
 * " · 1 plan waits for you" (or " · N plans wait for you" above one, nothing while 0) joined in
 * right after "Watching"; "Proactive VIKI is off" while off and "Paused until HH:MM" while
 * paused, neither ever showing that clause — each of the three with " · Focus until HH:MM"
 * appended when Focus is set, and " · rules only" appended when `model` is a rules-only state
 * (anything but "ready"). */
export function statusLine(view: StatusLineView, nowMs: number): string {
  let line: string;
  if (view.state === "off") {
    line = "Proactive VIKI is off";
  } else if (view.state === "paused") {
    line = view.paused_until !== null ? `Paused until ${localClock(view.paused_until)}` : "Paused";
  } else {
    const waiting =
      view.plans_waiting === 1
        ? " · 1 plan waits for you"
        : view.plans_waiting > 1
          ? ` · ${view.plans_waiting} plans wait for you`
          : "";
    const checked =
      view.checked_at !== null ? ` · checked ${relativeTime(view.checked_at, nowMs)}` : "";
    line = `Watching${waiting} · ${view.open} open${checked}`;
  }
  if (view.focus_until !== null) {
    line += ` · Focus until ${localClock(view.focus_until)}`;
  }
  if (RULES_ONLY_MODEL_STATES.has(view.model)) {
    line += " · rules only";
  }
  return line;
}
