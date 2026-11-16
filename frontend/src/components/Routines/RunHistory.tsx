/**
 * A routine's recorded runs, newest first, 20 at a time. "Show more" asks for the rows before the
 * last one shown. A new `routineId` or `refreshKey` reloads from the top; leaving or changing
 * the id aborts what is in flight. Every server string is a text node.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import type { RoutineRun, RoutinesApi } from "../../clients/automation/routinesApi";
import { en, type Key } from "../../i18n";
import { t, tf } from "../../i18n/t";
import { outcomeText, reasonText, stampLabel } from "../../state/routines";
import { Loading } from "../Loading/Loading";
import "./RoutinePreview.css";

const PAGE = 20;

export interface RunHistoryProps {
  api: RoutinesApi;
  routineId: string;
  refreshKey?: unknown;
  onOpenSession(sessionId: string): void;
}

function kindText(kind: string): string {
  const key = `routines.kind.${kind}`;
  return Object.hasOwn(en, key) ? t(key as Key) : kind;
}

function Row({ run, onOpenSession }: { run: RoutineRun; onOpenSession(id: string): void }) {
  const reason = run.attempt?.reason ?? run.reason;
  return (
    <li className="routine-history-row">
      <span className="mono">{stampLabel(run.scheduled_for ?? run.recorded_at)}</span>
      <span className="chip dim">{kindText(run.kind)}</span>
      <span>
        {outcomeText(run)}
        {reason ? ` (${reasonText(reason)})` : ""}
      </span>
      {run.revision !== null ? (
        <span className="routine-preview-note">
          {tf("routines.history.revision", { revision: run.revision })}
        </span>
      ) : null}
      {run.attempt ? (
        <button
          type="button"
          className="btn alt"
          onClick={() => onOpenSession((run.attempt as { session_id: string }).session_id)}
        >
          {t("routines.action.open_session")}
        </button>
      ) : null}
    </li>
  );
}

export function RunHistory({
  api,
  routineId,
  refreshKey,
  onOpenSession,
}: RunHistoryProps): ReactElement {
  const [runs, setRuns] = useState<RoutineRun[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [more, setMore] = useState(false);
  const pending = useRef<AbortController | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey only forces a reload
  useEffect(() => {
    load(undefined);
    return () => pending.current?.abort();
  }, [api, routineId, refreshKey]);

  function load(before: string | undefined): void {
    pending.current?.abort();
    const mine = new AbortController();
    pending.current = mine;
    setStatus("loading");
    if (before === undefined) {
      setRuns([]);
      setMore(false);
    }
    api
      .runs(routineId, { limit: PAGE, ...(before === undefined ? {} : { before }) }, mine.signal)
      .then((page) => {
        if (mine.signal.aborted) return;
        setRuns((current) => (before === undefined ? page : [...current, ...page]));
        setMore(page.length === PAGE);
        setStatus("ready");
      })
      .catch(() => {
        if (!mine.signal.aborted) setStatus("error");
      });
  }

  const last = runs.at(-1);
  return (
    <section className="routine-history">
      <h3>{t("routines.history.title")}</h3>
      {runs.length > 0 ? (
        <ul>
          {runs.map((run) => (
            <Row key={run.occurrence_id} run={run} onOpenSession={onOpenSession} />
          ))}
        </ul>
      ) : null}
      {status === "loading" ? <Loading>{t("routines.loading")}</Loading> : null}
      {status === "error" ? <p role="alert">{t("routines.error.generic")}</p> : null}
      {status === "ready" && runs.length === 0 ? <p>{t("routines.history.empty")}</p> : null}
      {status !== "loading" && more && last ? (
        <button type="button" className="btn txt" onClick={() => load(last.occurrence_id)}>
          {t("routines.history.more")}
        </button>
      ) : null}
    </section>
  );
}
