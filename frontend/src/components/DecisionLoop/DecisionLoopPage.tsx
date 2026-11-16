/**
 * The Decision page: the page head, the Proactive VIKI card, the How VIKI works strip and the rows
 * in five groups, over the row kit (`DecisionRow.tsx`). This file owns only assembly and `page`'s
 * own fetch/refresh cycle; every row's own fields, controls and copy live in its own file
 * (`rows/*.tsx`).
 *
 * Loading: `api.page()` runs once on mount, and again — through the very same `load` — after
 * every row's `onRefresh`, since a change a row made through `patch` or `api` changes a fact only
 * the page's own view carries (`patch` and the per-item API calls answer their own changed
 * document, never the Decision page view). A `requestId` ref drops an answer that is no longer
 * the latest one asked for (two overlapping loads can settle in either order); the one
 * `AbortController`, created on the first load and reused by every later one, is aborted only
 * once, on unmount, matching `components/Data/DataView.tsx`'s own "jsdom has no scrollIntoView"
 * note for the same reason: real navigation can unmount this page mid-request, and a real browser
 * is the only place that abort actually cancels anything.
 *
 * States: loading (`page` not yet fetched) shows the loading kit; a failed fetch shows "Decision
 * is unavailable right now." with Retry, which simply calls `load` again; both keep the page head
 * without its intro. Otherwise the page shows one read-only notice while settings cannot change
 * (the rows carry none), the card, the strip and the groups. The card and the strip are not rows:
 * they have no disclosure and no entry in the open record.
 *
 * Open state: one record, keyed by row id, every row closed by default. `openRow` (the
 * launcher's "Open Decision" and later deep links) opens a row id and scrolls its heading into
 * view once the rows exist, so "proactive", the card's id, scrolls to the card and opens nothing.
 * A How VIKI works button does the same through `jumpTo`, and also moves focus to the row's
 * disclosure. How VIKI is doing links open Data and focus its selected heading. Scrolling is skipped in jsdom (`scrollIntoView` is undefined there, the same
 * optional-chained call `DataView.tsx` and `FileView.tsx` already use).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { DecisionLoopApi, PageView } from "../../clients/decisionLoop/types";
import { t } from "../../i18n/t";
import { Loading } from "../Loading/Loading";
import "./DecisionLoopPage.css";
import { HowItWorks } from "./HowItWorks";
import { type DataFocus, DataRow } from "./rows/DataRows";
import { DoRow, HowRow } from "./rows/DoingRows";
import { LearningRow } from "./rows/LearningRows";
import { ProactiveCard } from "./rows/ProactiveCard";
import { ReachRow, WatchRow } from "./rows/ReachRows";
import { ModelRow, PrivacyRow } from "./rows/SetupRows";

export interface DecisionLoopPageProps {
  api: DecisionLoopApi;
  values: Readonly<Record<string, unknown>>;
  writable: boolean;
  patch: (changes: Record<string, unknown>) => Promise<unknown>;
  onOpenUrl: (url: string) => void;
  onOpenContext: () => void;
  onOpenChat: (sessionId: string) => void;
  /** Opens and scrolls to one row on arrival — the launcher's "Open Decision" and deep links. */
  openRow?: string;
}

/** Every row in page order (Learning sits between How VIKI is doing and Data); also this page's
 * `open` record's keys. */
const ROW_IDS = ["watch", "model", "privacy", "reach", "do", "how", "learning", "data"] as const;
type RowId = (typeof ROW_IDS)[number];

function isRowId(value: string): value is RowId {
  return (ROW_IDS as readonly string[]).includes(value);
}

/** Every row closed. */
function defaultOpen(): Record<RowId, boolean> {
  return {
    watch: false,
    model: false,
    privacy: false,
    reach: false,
    do: false,
    how: false,
    learning: false,
    data: false,
  };
}

function PageHead({ intro }: { intro: boolean }) {
  return (
    <header className="page-head">
      <h1 className="page-title">Decision</h1>
      {intro ? <p className="page-intro">{t("decision_page.intro")}</p> : null}
    </header>
  );
}

/** A group's title: its step number (decoration, so hidden from the accessible name), if any,
 * then its name. */
function GroupTitle({ id, step, children }: { id: string; step?: number; children: string }) {
  return (
    <h2 className="dloop-group-title" id={id}>
      {step === undefined ? null : (
        <span className="dloop-step-no" aria-hidden="true">
          {step}
        </span>
      )}
      {children}
    </h2>
  );
}

export function DecisionLoopPage({
  api,
  values,
  writable,
  patch,
  onOpenUrl,
  onOpenContext,
  onOpenChat,
  openRow,
}: DecisionLoopPageProps) {
  const [page, setPage] = useState<PageView | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<Record<RowId, boolean>>(() => {
    const initial = defaultOpen();
    if (openRow && isRowId(openRow)) initial[openRow] = true;
    return initial;
  });
  const [dataFocus, setDataFocus] = useState<DataFocus | undefined>();
  const controllerRef = useRef<AbortController | null>(null);
  const latestRequest = useRef(0);

  const load = useCallback(() => {
    let controller = controllerRef.current;
    if (!controller) {
      controller = new AbortController();
      controllerRef.current = controller;
    }
    const requestId = ++latestRequest.current;
    api.page(controller.signal).then(
      (next) => {
        if (requestId !== latestRequest.current) return; // an older load lost the race
        setPage(next);
        setFailed(false);
      },
      () => {
        if (requestId !== latestRequest.current || controller.signal.aborted) return;
        setPage(null);
        setFailed(true);
      },
    );
  }, [api]);

  useEffect(() => {
    load();
    return () => controllerRef.current?.abort();
  }, [load]);

  function toggleRow(id: RowId): void {
    setOpen((current) => ({ ...current, [id]: !current[id] }));
  }

  useEffect(() => {
    if (!openRow || !isRowId(openRow)) return;
    setOpen((current) => (current[openRow] ? current : { ...current, [openRow]: true }));
  }, [openRow]);

  function jumpTo(row: RowId): void {
    setOpen((current) => (current[row] ? current : { ...current, [row]: true }));
    document.getElementById(`${row}-heading`)?.scrollIntoView?.({ block: "start" });
    document.querySelector<HTMLElement>(`#${row}-heading .dloop-disclosure`)?.focus();
  }

  function openData(target: DataFocus["target"]): void {
    setOpen((current) => (current.data ? current : { ...current, data: true }));
    setDataFocus((current) => ({ target, seq: (current?.seq ?? 0) + 1 }));
    document.getElementById("data-heading")?.scrollIntoView?.({ block: "start" });
  }

  const rowsMounted = page !== null;
  useEffect(() => {
    if (!openRow || !rowsMounted) return;
    document.getElementById(`${openRow}-heading`)?.scrollIntoView?.({ block: "start" });
  }, [openRow, rowsMounted]);

  if (failed) {
    return (
      <div className="dloop-page">
        <PageHead intro={false} />
        <p role="alert" className="dloop-error">
          Decision is unavailable right now.
        </p>
        <div className="dloop-button-row">
          <button type="button" className="btn alt" onClick={load}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (page === null) {
    return (
      <div className="dloop-page">
        <PageHead intro={false} />
        <Loading>Loading Decision…</Loading>
      </div>
    );
  }

  const rowProps = { page, values, writable, patch, api, onRefresh: load };

  return (
    <div className="dloop-page">
      <PageHead intro />
      {!writable ? <p className="dloop-hint">{t("decision_page.read_only")}</p> : null}
      <ProactiveCard page={page} writable={writable} patch={patch} onRefresh={load} />
      <HowItWorks enabled={page.enabled} onJump={jumpTo} />
      <section className="dloop-group" aria-labelledby="decision-notices">
        <GroupTitle id="decision-notices" step={1}>
          {t("decision_page.group.notices")}
        </GroupTitle>
        <WatchRow {...rowProps} open={open.watch} onToggle={() => toggleRow("watch")} />
      </section>
      <section className="dloop-group" aria-labelledby="decision-decides">
        <GroupTitle id="decision-decides" step={2}>
          {t("decision_page.group.decides")}
        </GroupTitle>
        <ModelRow
          {...rowProps}
          onOpenUrl={onOpenUrl}
          onOpenContext={onOpenContext}
          open={open.model}
          onToggle={() => toggleRow("model")}
        />
        <PrivacyRow
          {...rowProps}
          onOpenUrl={onOpenUrl}
          onOpenContext={onOpenContext}
          open={open.privacy}
          onToggle={() => toggleRow("privacy")}
        />
      </section>
      <section className="dloop-group" aria-labelledby="decision-tells">
        <GroupTitle id="decision-tells" step={3}>
          {t("decision_page.group.tells")}
        </GroupTitle>
        <ReachRow {...rowProps} open={open.reach} onToggle={() => toggleRow("reach")} />
      </section>
      <section className="dloop-group" aria-labelledby="decision-actions">
        <GroupTitle id="decision-actions" step={4}>
          Work VIKI can start
        </GroupTitle>
        <DoRow
          {...rowProps}
          onOpenChat={onOpenChat}
          open={open.do}
          onToggle={() => toggleRow("do")}
        />
      </section>
      <section className="dloop-group" aria-labelledby="decision-history">
        <GroupTitle id="decision-history">{t("decision_page.group.history")}</GroupTitle>
        <HowRow
          {...rowProps}
          onOpenData={openData}
          open={open.how}
          onToggle={() => toggleRow("how")}
        />
        <LearningRow {...rowProps} open={open.learning} onToggle={() => toggleRow("learning")} />
        <DataRow
          {...rowProps}
          onJump={jumpTo}
          onOpenContext={onOpenContext}
          focus={dataFocus}
          open={open.data}
          onToggle={() => toggleRow("data")}
        />
      </section>
    </div>
  );
}
