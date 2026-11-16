/**
 * One routine: its state actions, the live run (when there is one), the rehearsal and the run
 * history. State actions send `state_revision`, runs send `current_revision`. A command id is kept
 * per (action, routine, revision): reused after a failure so a retry is the same command, dropped
 * after success. Every server string is a text node.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import {
  type RoutineDetailData,
  type RoutineStateAction,
  type RoutinesApi,
  RoutinesApiError,
} from "../../clients/automation/routinesApi";
import { en, type Key } from "../../i18n";
import { t, tf } from "../../i18n/t";
import {
  type Attention,
  cardSeat,
  changedFolder,
  reasonText,
  stateText,
  whenLabel,
} from "../../state/routines";
import { Loading } from "../Loading/Loading";
import { RoutinePreview } from "./RoutinePreview";
import { RunHistory } from "./RunHistory";
import "./RoutineDetail.css";

export interface RoutineDetailProps {
  api: RoutinesApi;
  routineId: string;
  nowMs: number;
  attention: Attention | null;
  refreshKey?: unknown;
  onEdit(detail: RoutineDetailData): void;
  onOpenSession(sessionId: string): void;
  onStop(sessionId: string): void;
  onChanged(): void;
  /** Injected so tests need no dialog. */
  confirm?(message: string): boolean;
}

type Action = "edit" | "test" | "enable" | "pause" | "run_now" | "archive";

const ACTIONS: Record<RoutineDetailData["state"], Action[]> = {
  draft: ["edit", "test", "archive"],
  tested: ["edit", "enable", "test", "archive"],
  enabled: ["edit", "pause", "run_now", "archive"],
  paused: ["edit", "enable", "run_now", "archive"],
  archived: [],
};

type Failure = { blockers: string[] } | { text: string };

function failure(error: unknown): Failure {
  if (error instanceof RoutinesApiError) {
    if (error.status === 422 && error.blockers.length > 0)
      return { blockers: error.blockers.map(reasonText) };
    const key = `routines.error.${error.code}`;
    if (Object.hasOwn(en, key)) return { text: t(key as Key) };
  }
  return { text: t("routines.error.generic") };
}

export function RoutineDetail({
  api,
  routineId,
  nowMs,
  attention,
  refreshKey,
  onEdit,
  onOpenSession,
  onStop,
  onChanged,
  confirm = (message) => window.confirm(message),
}: RoutineDetailProps): ReactElement {
  const [loaded, setLoaded] = useState<RoutineDetailData | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [failed, setFailed] = useState<Failure | null>(null);
  const commandIds = useRef(new Map<string, string>());

  // biome-ignore lint/correctness/useExhaustiveDependencies: reload and refreshKey only force a read
  useEffect(() => {
    const mine = new AbortController();
    setLoadError(false);
    api
      .get(routineId, mine.signal)
      .then((data) => {
        if (!mine.signal.aborted) setLoaded(data);
      })
      .catch(() => {
        if (!mine.signal.aborted) setLoadError(true);
      });
    return () => mine.abort();
  }, [api, routineId, reload, refreshKey]);

  async function act(action: Exclude<Action, "edit">, current: RoutineDetailData): Promise<void> {
    if (action === "archive" && !confirm(t("routines.action.archive_confirm"))) return;
    const revision =
      action === "test" || action === "run_now" ? current.current_revision : current.state_revision;
    const key = `${action}:${current.routine_id}:${revision}`;
    let id = commandIds.current.get(key);
    if (id === undefined) {
      id = crypto.randomUUID();
      commandIds.current.set(key, id);
    }
    setBusy(true);
    setAccepted(false);
    setFailed(null);
    try {
      if (action === "test" || action === "run_now")
        await api.run(current.routine_id, id, revision);
      else await api.setState(current.routine_id, action as RoutineStateAction, id, revision);
      commandIds.current.delete(key);
      setAccepted(true);
      onChanged();
      setReload((n) => n + 1);
    } catch (error) {
      setFailed(failure(error));
    } finally {
      setBusy(false);
    }
  }

  // A detail loaded for another routine is never shown or acted on.
  const detail = loaded?.routine_id === routineId ? loaded : null;
  const readFailure = (
    <>
      <p role="alert">
        {detail
          ? "Refresh failed. Showing the last loaded routine details."
          : t("routines.error.generic")}
      </p>
      <button type="button" className="btn alt" onClick={() => setReload((n) => n + 1)}>
        Retry
      </button>
    </>
  );
  if (loadError && !detail) return readFailure;
  if (!detail) return <Loading>{t("routines.loading")}</Loading>;

  const active = detail.active_run;
  return (
    <section className="routine-detail">
      {loadError && readFailure}
      <h2>{detail.name}</h2>
      {detail.document.purpose ? (
        <p className="routine-detail-purpose">{detail.document.purpose}</p>
      ) : null}
      <p>
        <span className="chip dim">{stateText(detail.state)}</span>
      </p>
      <div className="routine-detail-actions">
        {ACTIONS[detail.state].map((action) =>
          action === "edit" ? (
            <button key={action} type="button" className="btn alt" onClick={() => onEdit(detail)}>
              {t("routines.action.edit")}
            </button>
          ) : (
            <button
              key={action}
              type="button"
              className="btn alt"
              disabled={busy}
              onClick={() => void act(action, detail)}
            >
              {t(`routines.action.${action}`)}
            </button>
          ),
        )}
      </div>
      {accepted ? <p role="status">{t("routines.action.accepted")}</p> : null}
      {failed && "blockers" in failed ? (
        <div role="alert">
          <p>{t("routines.not_ready")}</p>
          <ul>
            {failed.blockers.map((text, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a fixed list rebuilt on each failure
              <li key={index}>{text}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {failed && "text" in failed ? <p role="alert">{failed.text}</p> : null}
      {active ? (
        <section className="routine-detail-run" aria-label={t("routines.outcome.running")}>
          <h3>{t("routines.outcome.running")}</h3>
          {active.stop_by ? (
            <p>{tf("routines.run.stop_by", { time: whenLabel(active.stop_by, nowMs) })}</p>
          ) : null}
          {attention?.kind === "waiting" ? (
            <p>
              {attention.stopBy
                ? tf("routines.attention.waiting", { time: whenLabel(attention.stopBy, nowMs) })
                : t("routines.attention.waiting_untimed")}
            </p>
          ) : null}
          <button
            type="button"
            className="btn alt"
            onClick={() => onOpenSession(active.session_id)}
          >
            {t("routines.action.open_session")}
          </button>
          <button type="button" className="btn alt" onClick={() => onStop(active.session_id)}>
            {t("routines.action.stop")}
          </button>
        </section>
      ) : null}
      {attention?.kind === "blocked" ? (
        <p>
          {attention.reason === "needs_decision"
            ? t("routines.attention.needs_decision")
            : tf("routines.attention.blocked", { reason: reasonText(attention.reason) })}
        </p>
      ) : null}
      {attention?.kind === "expired" ? <p>{t("routines.attention.expired")}</p> : null}
      <RoutinePreview
        preview={detail}
        kind={detail.document.schedule.kind}
        scheduleZone={detail.document.schedule.timezone}
        cardSeat={cardSeat(detail.document)}
        changedFolder={changedFolder(detail.document)}
        stopAtDraft={detail.document.on_confirmation === "stop"}
        collapsePrompt
      />
      <RunHistory
        api={api}
        routineId={routineId}
        refreshKey={`${refreshKey}:${reload}`}
        onOpenSession={onOpenSession}
      />
    </section>
  );
}
