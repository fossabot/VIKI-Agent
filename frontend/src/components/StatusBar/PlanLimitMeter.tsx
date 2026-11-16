/**
 * The status bar's plan-limit meter: fetches the newest plan-limit reading and
 * shows the 5-hour window's use (then the weekly window's, if also reported), or a bare
 * "Plan limit · resets …" when neither named window matches what is currently limiting.
 * Renders nothing until a reading has landed, and nothing when the reading carries no window
 * or reset data worth showing.
 */
import { useEffect, useState } from "react";
import type {
  LimitsApi,
  PlanLimitReading,
  PlanWindow,
} from "../../clients/conversations/limitsApi";

export interface PlanLimitMeterProps {
  api: LimitsApi;
  refreshKey?: unknown;
  nowMs?: number;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;
const DAY_MS = 24 * 60 * 60 * 1000;

function pad2(n: number): string {
  return n < 10 ? `0${n}` : `${n}`;
}

function pct(u: number): number {
  return Math.min(100, Math.round(u * 100));
}

/** Local zero-padded HH:MM, or "Ddd HH:MM" when more than 24h from `nowMs`; also
 * words the Decision page's hold line (`DoRow`). */
export function formatT(ms: number, nowMs: number): string {
  const d = new Date(ms);
  const hhmm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  return Math.abs(ms - nowMs) > DAY_MS ? `${DAYS[d.getDay()]} ${hhmm}` : hhmm;
}

/** The "· NN% · resets T" / "· reset at T" suffix for one window. */
function windowPhrase(w: PlanWindow, nowMs: number): { text: string; future: boolean } {
  const future = w.resets_at !== null && w.resets_at * 1000 > nowMs;
  let text = "";
  if (w.utilization !== null && future) text += ` · ${pct(w.utilization)}%`;
  if (w.resets_at !== null) {
    const t = formatT(w.resets_at * 1000, nowMs);
    text += future ? ` · resets ${t}` : ` · reset at ${t}`;
  }
  return { text, future };
}

function buildText(reading: PlanLimitReading, nowMs: number): string | null {
  const five: PlanWindow | null =
    reading.windows.five_hour ??
    (reading.limiting === "five_hour"
      ? { utilization: reading.utilization, resets_at: reading.resets_at }
      : null);
  const week: PlanWindow | null =
    reading.windows.seven_day ??
    (reading.limiting === "seven_day"
      ? { utilization: reading.utilization, resets_at: reading.resets_at }
      : null);

  if (five !== null) {
    let out = `5-hour window${windowPhrase(five, nowMs).text}`;
    if (week !== null) {
      const weekly = windowPhrase(week, nowMs);
      if (week.utilization !== null && weekly.future) out += ` · weekly ${pct(week.utilization)}%`;
    }
    return out;
  }
  if (week !== null) return `Weekly window${windowPhrase(week, nowMs).text}`;
  if (reading.resets_at !== null) {
    return `Plan limit · resets ${formatT(reading.resets_at * 1000, nowMs)}`;
  }
  return null;
}

/** Fetches on mount and on every `refreshKey` change, aborting the fetch in flight. A failed
 * (or aborted) fetch keeps whatever reading is already shown. */
export function PlanLimitMeter({ api, refreshKey, nowMs = Date.now() }: PlanLimitMeterProps) {
  const [reading, setReading] = useState<PlanLimitReading | null | undefined>(undefined);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey is the caller's reload signal, not a value this effect's body reads.
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    api.get(controller.signal).then(
      (value) => {
        if (live) setReading(value);
      },
      () => {
        /* A failed (or aborted) fetch keeps the last reading. */
      },
    );
    return () => {
      live = false;
      controller.abort();
    };
  }, [api, refreshKey]);

  if (reading === undefined || reading === null) return null;
  const text = buildText(reading, nowMs);
  if (text === null) return null;

  const warning = reading.status === "allowed_warning";
  const rejected = reading.status === "rejected";
  const observedMs = reading.observed_at !== null ? Date.parse(reading.observed_at) : Number.NaN;
  const title =
    `Reported by Claude Code at ${formatT(Number.isNaN(observedMs) ? nowMs : observedMs, nowMs)}` +
    (rejected ? ". The limit is reached." : warning ? ". Close to the limit." : "");

  return (
    <span className="statusbar-limit" data-status={reading.status} title={title}>
      {text}
    </span>
  );
}
