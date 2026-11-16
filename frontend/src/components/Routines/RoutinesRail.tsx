/**
 * The right rail's Routines section, with an optional title: up to five live routines, the ones that
 * need the user first. Every server string (the name) is a text node.
 *
 * Rules:
 * - Archived routines are left out. Order: attention, then running, then enabled by next run,
 *   then the rest by name.
 * - The head count reads "Paused" (dim) while the scheduler is paused, else the attention count
 *   (orange) when above zero.
 * - Each row opens the routine: when (or the state when nothing is due), the name, and one chip:
 *   attention (orange), else running, else the last outcome (both dim), success included.
 * - `onRunNow`, when given, adds a "Run now" button to a row that is enabled or paused with no
 *   run already going. While it runs the button is disabled and reads "Starting…"; a failure
 *   leaves a note on the row until that row is clicked again.
 */
import { Fragment, type ReactElement, useState } from "react";
import type { RoutineItem } from "../../clients/automation/routinesApi";
import { t } from "../../i18n/t";
import {
  attentionCount,
  outcomeText,
  type RoutinesSnapshot,
  routineAttention,
  stateText,
  whenLabel,
} from "../../state/routines";
import type { SessionView } from "../../state/view";
import { RailSection } from "../RightRail/RailSection";
import "./RoutinesRail.css";

const MAX_ROWS = 5;

type Sessions = Readonly<Record<string, Pick<SessionView, "cards"> | undefined>>;

export interface RoutinesRailProps {
  title?: string;
  snapshot: RoutinesSnapshot;
  sessions: Sessions;
  nowMs: number;
  onOpen(routineId: string): void;
  onShowAll(): void;
  /** Starts the routine by hand. Omitted, no row offers it. */
  onRunNow?: (item: RoutineItem) => Promise<void>;
}

function rank(item: RoutineItem, sessions: Sessions): number {
  if (routineAttention(item, sessions)) return 0;
  if (item.active_run) return 1;
  return item.state === "enabled" ? 2 : 3;
}

function nextMs(item: RoutineItem): number {
  const ms = item.next_run ? Date.parse(item.next_run) : Number.NaN;
  return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
}

function byName(a: RoutineItem, b: RoutineItem): number {
  return a.name.localeCompare(b.name);
}

function ordered(items: readonly RoutineItem[], sessions: Sessions): RoutineItem[] {
  return items
    .filter((item) => item.state !== "archived")
    .map((item) => ({ item, rank: rank(item, sessions) }))
    .sort((a, b) => {
      if (a.rank !== b.rank) return a.rank - b.rank;
      if (a.rank === 2 && nextMs(a.item) !== nextMs(b.item))
        return nextMs(a.item) < nextMs(b.item) ? -1 : 1;
      return byName(a.item, b.item);
    })
    .map((entry) => entry.item);
}

/** Whether the rail offers a hand-run for this item: enabled or paused, nothing running yet. */
function canRunNow(item: RoutineItem): boolean {
  return (item.state === "enabled" || item.state === "paused") && !item.active_run;
}

function Chip({ item, sessions }: { item: RoutineItem; sessions: Sessions }): ReactElement | null {
  if (routineAttention(item, sessions))
    return <span className="chip">{t("routines.attention.chip")}</span>;
  if (item.active_run) return <span className="chip dim">{t("routines.outcome.running")}</span>;
  const last = item.last_run;
  if (last) return <span className="chip dim">{outcomeText(last)}</span>;
  return null;
}

export function RoutinesRail({
  title = t("routines.title"),
  snapshot,
  sessions,
  nowMs,
  onOpen,
  onShowAll,
  onRunNow,
}: RoutinesRailProps): ReactElement {
  const paused = snapshot.scheduler?.paused === true;
  const attention = attentionCount(snapshot.items, sessions);
  const rows = ordered(snapshot.items, sessions);
  const showing = rows.slice(0, MAX_ROWS);
  // Keyed by routine_id; a row missing here is idle. A "failed" entry clears on that row's next
  // click (either button).
  const [runState, setRunState] = useState<Record<string, "pending" | "failed">>({});

  function openRow(routineId: string): void {
    if (runState[routineId] === "failed") {
      setRunState((state) => {
        const next = { ...state };
        delete next[routineId];
        return next;
      });
    }
    onOpen(routineId);
  }

  async function runRow(item: RoutineItem): Promise<void> {
    if (!onRunNow) return;
    setRunState((state) => ({ ...state, [item.routine_id]: "pending" }));
    try {
      await onRunNow(item);
      setRunState((state) => {
        const next = { ...state };
        delete next[item.routine_id];
        return next;
      });
    } catch {
      setRunState((state) => ({ ...state, [item.routine_id]: "failed" }));
    }
  }

  return (
    <RailSection
      title={title}
      className="rail-section--routines"
      count={paused ? t("routines.rail.paused") : attention > 0 ? attention : undefined}
      dim={paused}
    >
      {showing.map((item) => (
        <Fragment key={item.routine_id}>
          <div className="rt routines-rail-row">
            <button
              type="button"
              className="routines-rail-open"
              onClick={() => openRow(item.routine_id)}
            >
              <span className="h mono">
                {item.state === "enabled" && item.next_run
                  ? whenLabel(item.next_run, nowMs)
                  : stateText(item.state)}
              </span>
              <span className="routines-rail-name">{item.name}</span>
              <Chip item={item} sessions={sessions} />
            </button>
            {onRunNow && canRunNow(item) ? (
              <button
                type="button"
                className="routines-rail-run"
                aria-label={`Run ${item.name} now`}
                disabled={runState[item.routine_id] === "pending"}
                onClick={() => void runRow(item)}
              >
                {runState[item.routine_id] === "pending"
                  ? "Starting…"
                  : t("routines.action.run_now")}
              </button>
            ) : null}
          </div>
          {runState[item.routine_id] === "failed" ? (
            <p className="routines-rail-note" role="alert">
              Could not start this routine. Open it for details.
            </p>
          ) : null}
        </Fragment>
      ))}
      {showing.length === 0 && snapshot.status === "error" ? (
        <p className="routines-rail-note" role="alert">
          {t("routines.load_failed")}
        </p>
      ) : null}
      {showing.length === 0 && snapshot.status === "ready" ? (
        <p className="routines-rail-note">{t("routines.rail.empty")}</p>
      ) : null}
      <button type="button" className="routines-rail-all" onClick={onShowAll}>
        {t("routines.rail.all")}
      </button>
      <p className="routines-rail-note">{t("routines.rail.app_open")}</p>
    </RailSection>
  );
}
