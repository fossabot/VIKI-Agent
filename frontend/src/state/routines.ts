/**
 * Routines as an external store for useSyncExternalStore, plus the pure rules and labels the
 * rail, the view and the notifications share.
 *
 * Store rules:
 * - The first snapshot is {status: "idle", items: [], scheduler: null, error: null}. `start()`
 *   polls at once and then `intervalMs` after each poll settles (the timer is re-armed, never an
 *   interval), until `stop()`. While the items hold an active run the wait is `activeIntervalMs`,
 *   so a run that ends without a live-session change clears soon. A failed poll judges by the
 *   last known items, since the run may still be going. `status` is "loading" only for the first read; later reads keep
 *   "ready" (or "error") until they settle.
 * - One request in flight. `refresh()` asked while one runs is folded into a single read after
 *   it; asked while stopped it does nothing.
 * - A failure keeps the last items and sets `error` to the API's code ("unavailable" for
 *   anything else); the next success clears it.
 * - `stop()` aborts the request, drops a queued refresh and clears the timer; the aborted read
 *   never commits.
 */

import {
  type RoutineDocument,
  type RoutineItem,
  type RoutineRun,
  type RoutinesApi,
  RoutinesApiError,
  type SchedulerStatus,
} from "../clients/automation/routinesApi";
import { en, type Key } from "../i18n";
import { t, tf } from "../i18n/t";
import type { SessionView } from "./view";

export interface RoutinesSnapshot {
  status: "idle" | "loading" | "ready" | "error";
  items: RoutineItem[];
  scheduler: SchedulerStatus | null;
  /** The last failure's code, cleared on success. */
  error: string | null;
}

export interface RoutinesTimers {
  set(callback: () => void, ms: number): number;
  clear(handle: number): void;
}

export interface RoutinesStoreOptions {
  intervalMs?: number;
  /** The wait after a read while any routine has an active run (default 5,000 ms). */
  activeIntervalMs?: number;
  timers?: RoutinesTimers;
}

export interface RoutinesStore {
  getSnapshot(): RoutinesSnapshot;
  subscribe(listener: () => void): () => void;
  start(): void;
  stop(): void;
  refresh(): void;
}

const windowTimers: RoutinesTimers = {
  set: (callback, ms) => window.setTimeout(callback, ms),
  clear: (handle) => window.clearTimeout(handle),
};

export function createRoutinesStore(
  api: RoutinesApi,
  {
    intervalMs = 60_000,
    activeIntervalMs = 5_000,
    timers = windowTimers,
  }: RoutinesStoreOptions = {},
): RoutinesStore {
  let snapshot: RoutinesSnapshot = { status: "idle", items: [], scheduler: null, error: null };
  const listeners = new Set<() => void>();
  let started = false;
  let request: AbortController | null = null;
  let handle: number | null = null;
  let again = false;

  function commit(patch: Partial<RoutinesSnapshot>): void {
    snapshot = { ...snapshot, ...patch };
    for (const listener of [...listeners]) listener();
  }

  function disarm(): void {
    if (handle !== null) timers.clear(handle);
    handle = null;
  }

  async function poll(): Promise<void> {
    disarm();
    const mine = new AbortController();
    request = mine;
    if (snapshot.status === "idle") commit({ status: "loading" });
    try {
      const { routines, scheduler } = await api.list(mine.signal);
      if (mine.signal.aborted) return;
      commit({ status: "ready", items: routines, scheduler, error: null });
    } catch (error) {
      if (mine.signal.aborted) return;
      commit({
        status: "error",
        error: error instanceof RoutinesApiError ? error.code : "unavailable",
      });
    }
    request = null;
    if (again) {
      again = false;
      void poll();
      return;
    }
    handle = timers.set(
      () => {
        handle = null;
        void poll();
      },
      snapshot.items.some((routine) => routine.active_run) ? activeIntervalMs : intervalMs,
    );
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    start() {
      if (started) return;
      started = true;
      void poll();
    },
    stop() {
      if (!started) return;
      started = false;
      again = false;
      request?.abort();
      request = null;
      disarm();
      if (snapshot.status === "loading") commit({ status: "idle" });
    },
    refresh() {
      if (!started) return;
      if (request) again = true;
      else void poll();
    },
  };
}

export type Attention =
  | { kind: "waiting"; stopBy: string | null }
  | { kind: "blocked"; reason: string | null }
  | { kind: "expired" };

/** The app snapshot's sessions: a routine run is waiting when its session holds an open card. */
type SessionCards = Readonly<Record<string, Pick<SessionView, "cards"> | undefined>>;

/** First match wins. A paused or archived routine's last run needs nobody: the user has
 * already put the routine aside, so only a live waiting card still counts. The outcome is the
 * last attempt's, since a newer skip can stand in `last_run` while that run ends. */
export function routineAttention(item: RoutineItem, sessions: SessionCards): Attention | null {
  const active = item.active_run;
  if (active && (sessions[active.session_id]?.cards.length ?? 0) > 0)
    return { kind: "waiting", stopBy: active.stop_by };
  if (item.state === "paused" || item.state === "archived") return null;
  const attempt = item.last_attempt?.attempt;
  if (attempt?.outcome === "blocked") return { kind: "blocked", reason: attempt.reason };
  if (attempt?.outcome === "expired" && attempt.reason === "waiting_for_user")
    return { kind: "expired" };
  return null;
}

export function attentionCount(items: readonly RoutineItem[], sessions: SessionCards): number {
  return items.filter((item) => routineAttention(item, sessions) !== null).length;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

const clock = (d: Date): string => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const dayKey = (d: Date): string => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

/** The stamp's local time today, "tomorrow HH:MM" the next local day, else `M/D HH:MM`. A stamp
 * that does not parse gives "". */
export function whenLabel(stamp: string, nowMs: number): string {
  const at = new Date(stamp);
  if (Number.isNaN(at.getTime())) return "";
  const now = new Date(nowMs);
  if (dayKey(at) === dayKey(now)) return clock(at);
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  if (dayKey(at) === dayKey(tomorrow)) return tf("routines.when.tomorrow", { time: clock(at) });
  return stampLabel(stamp);
}

export function stampLabel(stamp: string): string {
  const at = new Date(stamp);
  if (Number.isNaN(at.getTime())) return "";
  return `${at.getMonth() + 1}/${at.getDate()} ${clock(at)}`;
}

/** The catalog's text for a server reason code; a code it does not know reads "other (code)". */
export function reasonText(code: string | null): string {
  const key = `routines.reason.${code}`;
  if (code && code !== "other" && Object.hasOwn(en, key)) return t(key as Key);
  return tf("routines.reason.other", { code: code ?? "unknown" });
}

export function stateText(state: RoutineItem["state"]): string {
  return t(`routines.state.${state}`);
}

/** The seat a routine's scheduled times wait for a todo card for, if they do. */
export function cardSeat(document: RoutineDocument): string | undefined {
  return document.run_only_if?.kind === "card" ? document.run_only_if.seat : undefined;
}

/** The folder whose changed notes a routine's scheduled times wait for, if they do. */
export function changedFolder(document: RoutineDocument): string | undefined {
  return document.run_only_if?.kind === "changed" ? document.run_only_if.folder : undefined;
}

const OUTCOMES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "expired",
  "blocked",
  "unknown",
  "skipped",
  "running",
]);

/** The result chip text: a finished attempt's outcome; an attempt still going or a claimed
 * occurrence is "running"; an occurrence recorded without an attempt is "skipped". */
export function outcomeText(run: RoutineRun): string {
  const outcome = run.attempt
    ? (run.attempt.outcome ?? "running")
    : run.state === "skipped"
      ? "skipped"
      : "running";
  if (outcome === "blocked" && run.attempt?.reason === "needs_decision") {
    return t("routines.outcome.needs_decision");
  }
  return t(`routines.outcome.${OUTCOMES.has(outcome) ? outcome : "unknown"}` as Key);
}
