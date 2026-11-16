/**
 * The Routines tab: notices, controls, the routine list and one pane (detail, editor or nothing).
 * Leaving a dirty editor (Cancel, another routine, New, Back) asks `routines.editor.discard`
 * first. The editor is keyed per routine revision and per create session because it reads its
 * `initial` once. Every server string is a text node.
 */
import { type ReactElement, type ReactNode, useEffect, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type {
  RoutineDetailData,
  RoutineItem,
  RoutinesApi,
} from "../../clients/automation/routinesApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import { t } from "../../i18n/t";
import {
  outcomeText,
  type RoutinesSnapshot,
  routineAttention,
  stateText,
  whenLabel,
} from "../../state/routines";
import { hasUnsavedWork } from "../../state/unsavedWork";
import type { SessionView } from "../../state/view";
import { Loading } from "../Loading/Loading";
import { RoutineControls, type RoutineControlValues } from "./RoutineControls";
import { RoutineDetail } from "./RoutineDetail";
import { RoutineEditor } from "./RoutineEditor";
import "./RoutinesView.css";

export interface RoutinesViewProps {
  api: RoutinesApi;
  snapshot: RoutinesSnapshot;
  sessions: Readonly<Record<string, Pick<SessionView, "cards"> | undefined>>;
  nowMs: number;
  selectedId: string | null;
  onSelect(routineId: string): void;
  onRefresh(): void;
  onBack(): void;
  renderBack?: (onBack: () => void) => ReactNode;
  profiles?: ProfileApi | undefined;
  workspaces: readonly WorkspaceJson[];
  activeWorkspaceId: string;
  controls: RoutineControlValues;
  onSettings(changes: Record<string, boolean | string | null>): Promise<void>;
  onRequestPermission(): Promise<void>;
  onOpenSession(sessionId: string): void;
  onStop(sessionId: string): void;
  /** Injected so tests need no dialog. */
  confirm?(message: string): boolean;
}

type Pane =
  | { kind: "detail" }
  | { kind: "create"; n: number }
  | { kind: "edit"; initial: RoutineDetailData };

function refreshKey(item: RoutineItem | undefined): string {
  if (!item) return "";
  const run = item.last_run;
  return [
    item.current_revision,
    item.state_revision,
    item.active_run?.attempt_id ?? "",
    item.active_run?.state ?? "",
    run?.occurrence_id ?? "",
    run?.attempt?.outcome ?? "",
  ].join("|");
}

export function RoutinesView({
  api,
  snapshot,
  sessions,
  nowMs,
  selectedId,
  onSelect,
  onRefresh,
  onBack,
  renderBack,
  profiles,
  workspaces,
  activeWorkspaceId,
  controls,
  onSettings,
  onRequestPermission,
  onOpenSession,
  onStop,
  confirm = (message) => window.confirm(message),
}: RoutinesViewProps): ReactElement {
  const [pane, setPane] = useState<Pane>({ kind: "detail" });
  const previousId = useRef(selectedId);
  useEffect(() => {
    if (previousId.current !== selectedId && renderBack) setPane({ kind: "detail" });
    previousId.current = selectedId;
  }, [selectedId, renderBack]);
  const creates = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);

  /** Runs `then` unless a dirty editor is open and the user declines to discard it. */
  function leave(then: () => void): void {
    if (pane.kind !== "detail" && hasUnsavedWork() && !confirm(t("routines.editor.discard")))
      return;
    then();
  }

  const { scheduler, items, status } = snapshot;
  const groups = {
    "Needs you": [] as RoutineItem[],
    Scheduled: [] as RoutineItem[],
    "Not scheduled": [] as RoutineItem[],
    Archived: [] as RoutineItem[],
  };
  for (const item of items) {
    const group =
      item.state === "archived"
        ? "Archived"
        : routineAttention(item, sessions)
          ? "Needs you"
          : item.next_run
            ? "Scheduled"
            : "Not scheduled";
    groups[group].push(item);
  }
  const selected = items.find((item) => item.routine_id === selectedId);

  function row(item: RoutineItem): ReactElement {
    const attention = routineAttention(item, sessions);
    const last = item.last_run;
    return (
      <li
        key={`routine-${item.routine_id}`}
        className={`routines-row${item.state === "archived" ? " routines-row--archived" : ""}${item.routine_id === selectedId ? " routines-row--selected" : ""}${attention ? " routines-row--attention" : ""}`}
      >
        <span className="routines-row-next">
          <span className="routines-row-clock" aria-hidden={item.next_run ? undefined : true}>
            {item.next_run ? whenLabel(item.next_run, nowMs) : "—"}
          </span>
          {!item.next_run ? (
            <span className="routines-row-time-label">{t("routines.next_none")}</span>
          ) : null}
          <span className="routines-row-time-label">
            {item.next_run
              ? "Next · local time"
              : item.schedule.kind === "manual"
                ? "Manual"
                : "No next run"}
          </span>
        </span>
        <button
          type="button"
          className="routines-row-name"
          aria-current={item.routine_id === selectedId ? "true" : undefined}
          onClick={() =>
            leave(() => {
              setPane({ kind: "detail" });
              onSelect(item.routine_id);
            })
          }
        >
          {item.name}
        </button>
        <span className="routines-row-status">
          <span className="chip dim">{stateText(item.state)}</span>
          {last && <span className="chip dim">{outcomeText(last)}</span>}
          {attention && <span className="chip">{t("routines.attention.chip")}</span>}
        </span>
      </li>
    );
  }

  const edited = pane.kind === "edit" ? pane.initial : null;
  return (
    <section className="routines-view" aria-label={t("routines.title")}>
      {renderBack?.(() =>
        leave(() => {
          setPane({ kind: "detail" });
          onBack();
        }),
      ) ?? (
        <button type="button" className="btn alt" onClick={() => leave(onBack)}>
          {t("routines.action.back")}
        </button>
      )}
      <header className="page-head">
        <h1 className="page-title" ref={heading} tabIndex={-1}>
          {t("routines.title")}
        </h1>
      </header>
      <section className="routines-scheduler" aria-label="Scheduling">
        <strong>
          {scheduler
            ? `${status === "error" ? "Last known: scheduler" : "Scheduler"} ${scheduler.state === "running" ? "running" : "not running"}`
            : "Scheduler status unavailable"}
        </strong>
        <p className="routines-note">Routines run only while VIKI Agent is open.</p>
        <details className="routines-policy">
          <summary>Scheduling details</summary>
          <p>{t("routines.app_open")}</p>
        </details>
      </section>
      {scheduler && scheduler.state !== "running" && (
        <p className="routines-note">{t("routines.scheduler_off")}</p>
      )}
      {scheduler?.paused && <p className="routines-note">{t("routines.paused_notice")}</p>}
      <RoutineControls
        scheduler={scheduler}
        controls={controls}
        onSettings={onSettings}
        onRequestPermission={onRequestPermission}
        onRefresh={onRefresh}
      />
      <div className="routines-body">
        <div className="routines-list">
          <button
            type="button"
            className="btn go"
            onClick={() =>
              leave(() => {
                creates.current += 1;
                setPane({ kind: "create", n: creates.current });
              })
            }
          >
            {t("routines.new")}
          </button>
          {items.length === 0 && status === "loading" && <Loading>{t("routines.loading")}</Loading>}
          {status === "error" && (
            <>
              <p role="alert">{t("routines.load_failed")}</p>
              {items.length > 0 && <p>Showing the last loaded routine list.</p>}
              <button type="button" className="btn alt" onClick={onRefresh}>
                Retry
              </button>
            </>
          )}
          {items.length === 0 && status === "ready" && <p>{t("routines.empty")}</p>}
          <ul>
            {Object.entries(groups).flatMap(([label, routines]) =>
              routines.length > 0
                ? [
                    <li key={`group-${label}`} className="routines-group" role="presentation">
                      <h2>{label}</h2>
                    </li>,
                    ...routines.map(row),
                  ]
                : [],
            )}
          </ul>
        </div>
        <div className="routines-pane">
          {pane.kind === "detail" && selectedId && (
            <RoutineDetail
              key={selectedId}
              api={api}
              routineId={selectedId}
              nowMs={nowMs}
              attention={selected ? routineAttention(selected, sessions) : null}
              refreshKey={refreshKey(selected)}
              onEdit={(initial) => setPane({ kind: "edit", initial })}
              onOpenSession={onOpenSession}
              onStop={onStop}
              onChanged={onRefresh}
            />
          )}
          {pane.kind !== "detail" && (
            <RoutineEditor
              key={
                edited
                  ? `edit-${edited.routine_id}-${edited.current_revision}`
                  : `new-${pane.kind === "create" ? pane.n : 0}`
              }
              api={api}
              initial={edited}
              {...(profiles ? { profiles } : {})}
              workspaces={workspaces}
              activeWorkspaceId={activeWorkspaceId}
              onSaved={(id) => {
                setPane({ kind: "detail" });
                onSelect(id);
                onRefresh();
              }}
              onCancel={() => leave(() => setPane({ kind: "detail" }))}
            />
          )}
        </div>
      </div>
    </section>
  );
}
