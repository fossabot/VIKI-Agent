/** Owned routines use the existing editor, rehearsal, controls and isolated run history. */
import { type ReactElement, useMemo, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type RoutineDetailData,
  type RoutinesApi,
  RoutinesApiError,
} from "../../clients/automation/routinesApi";
import type { BotRow } from "../../clients/messaging/botsApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import { t } from "../../i18n/t";
import {
  type RoutinesSnapshot,
  type RoutinesTimers,
  routineAttention,
  stateText,
  whenLabel,
} from "../../state/routines";
import { hasUnsavedWork } from "../../state/unsavedWork";
import type { SessionView } from "../../state/view";
import { Loading } from "../Loading/Loading";
import { RoutineControls, type RoutineControlValues } from "../Routines/RoutineControls";
import { RoutineDetail } from "../Routines/RoutineDetail";
import { RoutineEditor } from "../Routines/RoutineEditor";

export interface BotRoutinesProps {
  bot: BotRow;
  api: RoutinesApi;
  snapshot: RoutinesSnapshot;
  sessions: Readonly<Record<string, Pick<SessionView, "cards"> | undefined>>;
  nowMs: number;
  profiles?: ProfileApi;
  workspaces: readonly WorkspaceJson[];
  controls: RoutineControlValues;
  onSettings(changes: Record<string, boolean | string | null>): Promise<void>;
  onRequestPermission(): Promise<void>;
  /** Refresh both Bot ownership and the shared routine store. */
  onRefresh(): void;
  onOpenSession(sessionId: string): void;
  onStop(sessionId: string): void;
  confirm?(message: string): boolean;
  timers?: RoutinesTimers;
  /** Keep the draft editable while its current Bot context cannot admit actions. */
  blocked?: string;
}
type Pane =
  | { kind: "detail"; routineId: string | null }
  | { kind: "create"; n: number }
  | { kind: "edit"; initial: RoutineDetailData };

export function BotRoutines({
  bot,
  api,
  snapshot,
  sessions,
  nowMs,
  profiles,
  workspaces,
  controls,
  onSettings,
  onRequestPermission,
  onRefresh,
  onOpenSession,
  onStop,
  confirm = (message) => window.confirm(message),
  timers,
  blocked,
}: BotRoutinesProps): ReactElement {
  const [selection, setSelection] = useState<{ botId: string; pane: Pane }>({
    botId: bot.bot_id,
    pane: { kind: "detail", routineId: null },
  });
  const creates = useRef(0);
  const blockedRef = useRef(blocked);
  blockedRef.current = blocked;
  const owned = snapshot.items.filter((item) => bot.routine_ids.includes(item.routine_id));
  const savedPane = selection.botId === bot.bot_id ? selection.pane : null;
  const pane: Pane = savedPane ?? { kind: "detail", routineId: null };
  const movedDraft = pane.kind === "edit" && !bot.routine_ids.includes(pane.initial.routine_id);
  const selected =
    pane.kind === "detail" ? owned.find((item) => item.routine_id === pane.routineId) : undefined;
  const profileId = bot.profile_id;
  const workspaceId = bot.workspace_id;
  // The Bot roster and routine list can refresh separately. Check current ownership before
  // exposing actions on a detail whose document may have moved in the meantime.
  const ownedApi = useMemo<RoutinesApi>(
    () => ({
      ...api,
      async create(...args) {
        if (blockedRef.current) throw new RoutinesApiError(409, "conflict");
        return api.create(...args);
      },
      async revise(...args) {
        if (blockedRef.current) throw new RoutinesApiError(409, "conflict");
        return api.revise(...args);
      },
      async setState(...args) {
        if (blockedRef.current) throw new RoutinesApiError(409, "conflict");
        return api.setState(...args);
      },
      async run(...args) {
        if (blockedRef.current) throw new RoutinesApiError(409, "conflict");
        return api.run(...args);
      },
      async get(id, signal) {
        const detail = await api.get(id, signal);
        if (
          detail.routine_id !== id ||
          detail.document.profile_id !== profileId ||
          detail.document.workspace_id !== workspaceId
        )
          throw new RoutinesApiError(409, "conflict");
        return detail;
      },
    }),
    [api, profileId, workspaceId],
  );

  function select(next: Pane): void {
    if (blockedRef.current && (next.kind !== "detail" || next.routineId !== null)) return;
    if (pane.kind !== "detail" && hasUnsavedWork() && !confirm(t("routines.editor.discard")))
      return;
    setSelection({ botId: bot.bot_id, pane: next });
  }
  const canCreate = bot.profile !== null && !bot.profile.archived && bot.workspace !== null;
  const refreshKey = selected
    ? JSON.stringify([
        selected.current_revision,
        selected.state_revision,
        selected.active_run,
        selected.last_run,
        selected.last_attempt,
      ])
    : "";

  return (
    <section className="bots-routines" aria-label="Bot routines">
      <h2>Routines</h2>
      <p>Scheduled routines need an awake host and an open VIKI Agent window.</p>
      <p>
        {snapshot.scheduler?.state === "running"
          ? "Scheduler running"
          : "Scheduler unavailable or stopped"}
      </p>
      {snapshot.scheduler?.paused ? <p>{t("routines.paused_notice")}</p> : null}
      {blocked && pane.kind === "detail" ? <p role="alert">{blocked}</p> : null}
      <fieldset
        className="bots-routine-interactions"
        disabled={Boolean(blocked)}
        aria-label="Routine actions"
      >
        <details className="bots-routines-settings">
          <summary>Scheduling and notifications</summary>
          <p>These scheduling and notification settings apply to all routines.</p>
          <RoutineControls
            scheduler={snapshot.scheduler}
            controls={controls}
            onSettings={(changes) =>
              blockedRef.current
                ? Promise.reject(new RoutinesApiError(409, "conflict"))
                : onSettings(changes)
            }
            onRequestPermission={() =>
              blockedRef.current
                ? Promise.reject(new RoutinesApiError(409, "conflict"))
                : onRequestPermission()
            }
            onRefresh={onRefresh}
          />
        </details>
        <button
          type="button"
          className="btn go"
          disabled={!canCreate}
          onClick={() => {
            creates.current += 1;
            select({ kind: "create", n: creates.current });
          }}
        >
          New routine
        </button>
        {snapshot.status === "error" ? (
          <p role="alert">Routine refresh failed. Showing the last loaded list.</p>
        ) : null}
        <button type="button" className="btn alt" onClick={onRefresh}>
          Refresh routines
        </button>
        {owned.length === 0 ? (
          <p>
            {snapshot.status === "loading" || snapshot.status === "idle" ? (
              <Loading inline announce={false}>
                Loading routines…
              </Loading>
            ) : (
              "This Bot has no routines."
            )}
          </p>
        ) : (
          <ul className="bots-routines-list">
            {owned.map((item) => (
              <li key={item.routine_id}>
                <button
                  type="button"
                  className="btn txt"
                  aria-current={selected?.routine_id === item.routine_id ? "true" : undefined}
                  onClick={() => select({ kind: "detail", routineId: item.routine_id })}
                >
                  {item.name}
                </button>{" "}
                <span className="chip dim">{stateText(item.state)}</span>
                {item.next_run ? <span>{` · Next ${whenLabel(item.next_run, nowMs)}`}</span> : null}
              </li>
            ))}
          </ul>
        )}
        {pane.kind === "detail" && selected ? (
          <RoutineDetail
            key={`${bot.bot_id}:${selected.routine_id}`}
            api={ownedApi}
            routineId={selected.routine_id}
            nowMs={nowMs}
            attention={routineAttention(selected, sessions)}
            refreshKey={refreshKey}
            onEdit={(initial) => select({ kind: "edit", initial })}
            onOpenSession={onOpenSession}
            onStop={(id) => {
              if (!blockedRef.current) onStop(id);
            }}
            onChanged={onRefresh}
            confirm={confirm}
          />
        ) : null}
      </fieldset>
      {pane.kind !== "detail" ? (
        <RoutineEditor
          key={`${bot.bot_id}:${pane.kind === "create" ? `new:${pane.n}` : `${pane.initial.routine_id}:${pane.initial.current_revision}`}`}
          api={ownedApi}
          initial={pane.kind === "edit" ? pane.initial : null}
          {...(profiles ? { profiles } : {})}
          workspaces={workspaces}
          activeWorkspaceId={bot.workspace_id}
          {...(timers ? { timers } : {})}
          {...(blocked || movedDraft
            ? {
                saveBlocked:
                  blocked ??
                  "This routine is no longer owned by this Bot. Your draft is still open.",
              }
            : {})}
          {...(pane.kind === "create"
            ? {
                lockedOwner: {
                  botName: bot.profile?.identity.name ?? "Unavailable Bot",
                  profileId: bot.profile_id,
                  profileName: bot.profile?.identity.name ?? bot.profile_id,
                  workspaceId: bot.workspace_id,
                  workspaceName: bot.workspace?.name ?? bot.workspace_id,
                },
              }
            : {})}
          onSaved={(id) => {
            setSelection({ botId: bot.bot_id, pane: { kind: "detail", routineId: id } });
            onRefresh();
          }}
          onCancel={() => select({ kind: "detail", routineId: null })}
        />
      ) : null}
    </section>
  );
}
