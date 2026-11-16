import { useEffect, useRef, useState } from "react";
import {
  type DataKind,
  type DataSummaryView,
  DecisionLoopApiError,
} from "../../../clients/decisionLoop/types";
import { t, tf } from "../../../i18n/t";
import { ConfirmDialog, type ConfirmSection } from "../../Dialog/ConfirmDialog";
import { Loading } from "../../Loading/Loading";
import { Choice, DecisionRow, useRowOpen } from "../DecisionRow";
import { formatNumber } from "../wording";
import {
  ActivityChart,
  AnswerBars,
  type DataRange,
  InventoryCard,
  localDate,
  TopicTable,
} from "./DataParts";
import { DataRecords } from "./DataRecords";
import type { DecisionRowsProps } from "./DoingRows";

export type DataFocus = { target: "decisions" | "answers"; seq: number };

type Props = DecisionRowsProps & {
  onJump: (row: "learning" | "reach" | "model") => void;
  onOpenContext: () => void;
  focus?: DataFocus | undefined;
};

function dates(oldest: string | null, newest: string | null): string {
  return oldest === null || newest === null
    ? t("decision_page.data.dates.none")
    : tf("decision_page.data.dates", { oldest: localDate(oldest), newest: localDate(newest) });
}

function changed(value: string | null): string {
  return value === null
    ? t("decision_page.data.dates.none")
    : tf("decision_page.data.changed", { date: localDate(value) });
}

function isBusy(error: unknown): boolean {
  return error instanceof DecisionLoopApiError && error.code === "busy";
}

export function DataRow({
  page,
  writable,
  api,
  onRefresh,
  onJump,
  onOpenContext,
  focus,
  open: openProp,
  onToggle: onToggleProp,
}: Props) {
  const [open, toggle] = useRowOpen(false, openProp, onToggleProp);
  const [loadedSummary, setLoadedSummary] = useState<{
    api: typeof api;
    value: DataSummaryView;
  } | null>(null);
  const summary = loadedSummary?.api === api ? loadedSummary.value : null;
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<"busy" | "unavailable" | null>(null);
  const [retry, setRetry] = useState(0);
  const [range, setRange] = useState<DataRange>(30);
  const [kind, setKind] = useState<DataKind>("decisions");
  const [topic, setTopic] = useState<string | null>(null);
  const [confirmingApi, setConfirmingApi] = useState<typeof api | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleted, setDeleted] = useState<number | null>(null);
  const [recordDeletes, setRecordDeletes] = useState(0);
  const lastFocusSeq = useRef<number | undefined>(undefined);
  const requestedLoad = useRef<{ api: typeof api; retry: number } | null>(null);
  const currentApi = useRef(api);
  const mounted = useRef(false);
  const requestId = useRef(0);
  const previousApi = useRef(api);
  currentApi.current = api;
  const confirming = confirmingApi === api;
  const decisionCount = summary ? summary.decisions.decisions + summary.decisions.reports : 0;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (previousApi.current === api) return;
    previousApi.current = api;
    setConfirmingApi(null);
    setDeleteError(null);
    setDeleting(false);
    setDeleted(null);
  }, [api]);

  // A loaded summary does not stop a read: a refresh keeps it on screen while `retry` asks again.
  useEffect(() => {
    if (!open || (requestedLoad.current?.api === api && requestedLoad.current.retry === retry))
      return;
    requestedLoad.current = { api, retry };
    const thisRequest = ++requestId.current;
    const current = () =>
      mounted.current && currentApi.current === api && requestId.current === thisRequest;
    setLoading(true);
    setLoadError(null);
    api
      .dataSummary()
      .then((value) => {
        if (current()) setLoadedSummary({ api, value });
      })
      .catch((error: unknown) => {
        if (current()) setLoadError(isBusy(error) ? "busy" : "unavailable");
      })
      .finally(() => {
        if (current()) setLoading(false);
      });
  }, [api, open, retry]);

  // After the commit, not in the delete callback: the confirmation still traps focus there, and
  // it hands focus back to "Delete selected (0)", disabled by then, so focus would fall to <body>.
  useEffect(() => {
    if (recordDeletes > 0) document.getElementById("data-browse")?.focus();
  }, [recordDeletes]);

  useEffect(() => {
    if (!summary || !focus || focus.seq === lastFocusSeq.current) return;
    lastFocusSeq.current = focus.seq;
    if (focus.target === "decisions") {
      setKind("decisions");
      setTopic(null);
      document.getElementById("data-browse")?.focus();
    } else {
      document.getElementById("data-answers")?.focus();
    }
  }, [focus, summary]);

  async function deleteAll(): Promise<void> {
    setDeleting(true);
    setDeleteError(null);
    try {
      const result = await api.deleteEpisodes();
      if (currentApi.current !== api) return;
      setConfirmingApi(null);
      setDeleted(result.deleted);
      // Every count and listed record is now wrong, so they go until the new summary arrives.
      setLoadedSummary(null);
      setRetry((value) => value + 1);
      onRefresh();
    } catch {
      if (currentApi.current === api) setDeleteError(t("decision_page.data.all.failed"));
    } finally {
      if (currentApi.current === api) setDeleting(false);
    }
  }

  const sections: readonly ConfirmSection[] = summary
    ? [
        {
          heading: t("decision_page.data.all.removes"),
          items: [
            tf("decision_page.data.all.remove.decisions", {
              n: decisionCount,
            }),
            t("decision_page.data.all.remove.suggestions"),
            t("decision_page.data.all.remove.calls"),
            t("decision_page.data.all.remove.learned"),
          ],
        },
        {
          heading: t("decision_page.data.confirm.changes"),
          items: [
            "comes_back",
            "item_pauses",
            "today",
            "looks",
            "comparisons",
            "vault_change",
            "improve",
          ].map((key) => t(`decision_page.data.effect.${key}` as Parameters<typeof t>[0])),
        },
        {
          heading: t("decision_page.data.confirm.stays"),
          items: ["settings", "chats", "proposals", "today"].map((key) =>
            t(`decision_page.data.stay.${key}` as Parameters<typeof t>[0]),
          ),
        },
      ]
    : [];

  return (
    <DecisionRow
      id="data"
      name="Data"
      description={t("decision_page.data.description")}
      status={tf("decision_page.data.chip", { n: formatNumber(page.data.episodes) })}
      control={null}
      open={open}
      onToggle={toggle}
    >
      <div className="dloop-data">
        {loading && !summary ? <Loading>{t("decision_page.data.loading")}</Loading> : null}
        {loadError ? (
          <p role="alert">
            {t(`decision_page.data.${loadError}`)}{" "}
            <button
              type="button"
              className="btn txt sm"
              onClick={() => setRetry((value) => value + 1)}
            >
              {t("common.button.retry")}
            </button>
          </p>
        ) : null}
        {summary ? (
          <>
            <h3>{t("decision_page.data.kinds.title")}</h3>
            <p className="dloop-data-hint">{t("decision_page.data.kinds.hint")}</p>
            <div className="dloop-cards">
              <InventoryCard
                name={t("decision_page.data.kind.decisions.name")}
                meaning={t("decision_page.data.kind.decisions.meaning")}
                count={summary.decisions.decisions + summary.decisions.reports}
                dates={dates(summary.decisions.oldest, summary.decisions.newest)}
                limit={tf("decision_page.data.kind.decisions.limit", {
                  max: summary.decisions.max,
                  days: summary.decisions.retention_days,
                })}
                note={tf("decision_page.data.kind.decisions.count", summary.decisions)}
                meters={[
                  {
                    value: summary.decisions.decisions + summary.decisions.reports,
                    max: summary.decisions.max,
                    label: t("decision_page.data.kind.decisions.name"),
                  },
                ]}
                actions={[
                  {
                    label: t("decision_page.data.explore"),
                    onClick: () => {
                      setKind("decisions");
                      setTopic(null);
                      document.getElementById("data-browse")?.focus();
                    },
                  },
                ]}
              />
              <InventoryCard
                name={t("decision_page.data.kind.suggestions.name")}
                meaning={t("decision_page.data.kind.suggestions.meaning")}
                count={summary.suggestions.open + summary.suggestions.closed}
                dates={dates(summary.suggestions.oldest, summary.suggestions.newest)}
                limit={tf("decision_page.data.kind.suggestions.limit", {
                  max: summary.suggestions.max_open,
                  days: summary.suggestions.closed_days,
                })}
                note={tf("decision_page.data.kind.suggestions.count", summary.suggestions)}
                meters={[
                  {
                    value: summary.suggestions.open,
                    max: summary.suggestions.max_open,
                    label: t("decision_page.data.kind.suggestions.name"),
                  },
                ]}
                actions={[
                  {
                    label: t("decision_page.data.explore"),
                    onClick: () => {
                      setKind("suggestions");
                      setTopic(null);
                      document.getElementById("data-browse")?.focus();
                    },
                  },
                ]}
              />
              <InventoryCard
                name={t("decision_page.data.kind.calls.name")}
                meaning={t("decision_page.data.kind.calls.meaning")}
                count={summary.calls.count}
                dates={dates(summary.calls.oldest, summary.calls.newest)}
                limit={tf("decision_page.data.kind.calls.limit", { max: summary.calls.max })}
                note={tf("decision_page.data.kind.calls.count", { n: summary.calls.count })}
                meters={[
                  {
                    value: summary.calls.count,
                    max: summary.calls.max,
                    label: t("decision_page.data.kind.calls.name"),
                  },
                ]}
                actions={[
                  {
                    label: t("decision_page.data.explore"),
                    onClick: () => {
                      setKind("calls");
                      setTopic(null);
                      document.getElementById("data-browse")?.focus();
                    },
                  },
                ]}
              />
              <InventoryCard
                name={t("decision_page.data.kind.chats.name")}
                meaning={t("decision_page.data.kind.chats.meaning")}
                count={summary.chats.count}
                dates={dates(summary.chats.oldest, summary.chats.newest)}
                limit={t("decision_page.data.no_limit")}
                note={`${tf("decision_page.data.kind.chats.count", { n: summary.chats.count, running: summary.chats.running })} ${t("decision_page.data.kind.chats.kept")}`}
                actions={[]}
              />
              <InventoryCard
                name={t("decision_page.data.kind.proposals.name")}
                meaning={t("decision_page.data.kind.proposals.meaning")}
                count={summary.proposals.wordings + summary.proposals.changes}
                dates={dates(summary.proposals.oldest, summary.proposals.newest)}
                limit={tf("decision_page.data.kind.proposals.limit", {
                  wordings: summary.proposals.max_wordings,
                  changes: summary.proposals.max_changes,
                })}
                note={`${tf("decision_page.data.kind.proposals.count", summary.proposals)} ${t("decision_page.data.kind.proposals.kept")}`}
                meters={[
                  {
                    value: summary.proposals.wordings,
                    max: summary.proposals.max_wordings,
                    label: tf("decision_page.data.kind.proposals.count", {
                      wordings: summary.proposals.wordings,
                      changes: 0,
                    }),
                  },
                  {
                    value: summary.proposals.changes,
                    max: summary.proposals.max_changes,
                    label: tf("decision_page.data.kind.proposals.count", {
                      wordings: 0,
                      changes: summary.proposals.changes,
                    }),
                  },
                ]}
                actions={[]}
              />
              <InventoryCard
                name={t("decision_page.data.kind.preferences.name")}
                meaning={t("decision_page.data.kind.preferences.meaning")}
                count={summary.preferences.muted}
                dates={changed(summary.preferences.updated)}
                limit={t("decision_page.data.kind.preferences.limit")}
                note={`${tf("decision_page.data.kind.preferences.count", { muted: summary.preferences.muted })} ${summary.preferences.learned ? t("decision_page.data.kind.preferences.learned") : t("decision_page.data.kind.preferences.not_learned")} ${t("decision_page.data.kind.preferences.kept")}`}
                actions={[
                  {
                    label: t("decision_page.data.open_learning"),
                    onClick: () => onJump("learning"),
                  },
                  { label: t("decision_page.data.open_reach"), onClick: () => onJump("reach") },
                ]}
              />
              <InventoryCard
                name={t("decision_page.data.kind.key.name")}
                meaning={t("decision_page.data.kind.key.meaning")}
                count={summary.key.saved ? 1 : 0}
                dates={changed(summary.key.updated)}
                limit={t("decision_page.data.kind.key.limit")}
                note={`${summary.key.saved ? t("decision_page.data.kind.key.saved") : t("decision_page.data.kind.key.missing")} ${t("decision_page.data.kind.key.kept")}`}
                actions={[
                  { label: t("decision_page.data.open_model"), onClick: () => onJump("model") },
                ]}
              />
            </div>
            <h3>{t("decision_page.data.activity.title")}</h3>
            <Choice
              label={t("decision_page.data.range")}
              options={[30, 90, 365].map((value) => ({
                value: String(value),
                label: t(`decision_page.data.range.${value}` as Parameters<typeof t>[0]),
              }))}
              value={String(range)}
              onChange={(value) => setRange(Number(value) as DataRange)}
            />
            <ActivityChart days={summary.days} today={summary.today} range={range} />
            <h3 id="data-answers" tabIndex={-1}>
              {t("decision_page.data.answers.title")}
            </h3>
            <p className="dloop-data-hint">{t("decision_page.data.answers.hint")}</p>
            <AnswerBars days={summary.days} today={summary.today} range={range} />
            <h3>{t("decision_page.data.topics.title")}</h3>
            <p className="dloop-data-hint">{t("decision_page.data.topics.hint")}</p>
            <TopicTable
              topics={summary.topics}
              onTopic={(value) => {
                setKind("decisions");
                setTopic(value);
                document.getElementById("data-browse")?.focus();
              }}
            />
            <DataRecords
              api={api}
              asOf={summary.as_of}
              kind={kind}
              onKind={(value) => {
                setKind(value);
                setTopic(null);
              }}
              topic={topic}
              onTopic={setTopic}
              writable={writable}
              onDeleted={() => {
                // The summary stays mounted while it is read again: dropping it would unmount
                // this browser with its result line and filters.
                setRetry((value) => value + 1);
                setRecordDeletes((value) => value + 1);
                onRefresh();
              }}
              onOpenContext={onOpenContext}
            />
            <p>{tf("decision_page.data.as_of", { time: localDate(summary.as_of) })}</p>
            <p className="dloop-hint">{t("decision_page.data.all.hint")}</p>
            <button
              type="button"
              className="btn alt sm"
              disabled={!writable || decisionCount === 0}
              onClick={() => {
                setConfirmingApi(api);
                setDeleteError(null);
              }}
            >
              {t("decision_page.data.all.button")}
            </button>
            {confirming ? (
              <ConfirmDialog
                label={t("decision_page.data.all.confirm")}
                body={t("decision_page.data.all.body")}
                sections={sections}
                confirmLabel={t("decision_page.data.confirm.delete")}
                destructive
                irreversible
                busy={deleting || !writable}
                error={deleteError}
                onConfirm={() => void deleteAll()}
                onCancel={() => {
                  if (!deleting) setConfirmingApi(null);
                }}
              />
            ) : null}
          </>
        ) : null}
        {/* Outside the summary block: the result must show even when the read after it fails. */}
        {deleted !== null ? (
          <p role="status">{tf("decision_page.data.all.done", { n: deleted })}</p>
        ) : null}
      </div>
    </DecisionRow>
  );
}
