import { useEffect, useMemo, useRef, useState } from "react";
import {
  CANDIDATES,
  DATA_KINDS,
  DATA_SHOWN,
  type DataAnswer,
  type DataKind,
  type DataQuery,
  type DataRecordsView,
  type DataRecordView,
  type DataShown,
  type DecisionCandidate,
  type DecisionLevel,
  type DecisionLoopApi,
  DecisionLoopApiError,
  type DecisionTopic,
  FEEDBACK_HELD,
  FEEDBACK_SHOWN,
  ITEM_STATES,
  OUTCOMES,
  TOPICS,
} from "../../../clients/decisionLoop/types";
import { t, tf } from "../../../i18n/t";
import { ConfirmDialog, type ConfirmSection } from "../../Dialog/ConfirmDialog";
import { Loading } from "../../Loading/Loading";
import { Choice } from "../DecisionRow";
import { formatMoney, formatNumber, localClock, REASON_LABELS } from "../wording";
import { localDate, topicLabel } from "./DataParts";
import "./DataRecords.css";

type Props = {
  api: Pick<DecisionLoopApi, "dataRecords" | "dataRecord" | "deleteData" | "call">;
  asOf: string;
  kind: DataKind;
  onKind: (kind: DataKind) => void;
  topic: string | null;
  onTopic: (topic: string | null) => void;
  writable: boolean;
  onDeleted: () => void;
  onOpenContext: () => void;
  nowMs?: number;
};

type Row = DataRecordsView["rows"][number];
type Detail = DataRecordView | Awaited<ReturnType<DecisionLoopApi["call"]>>;
type PendingDelete = { query: DataQuery; count: number; matching: boolean; kind: DataKind };

const ANSWERS: readonly DataAnswer[] = [...FEEDBACK_SHOWN, ...FEEDBACK_HELD, "none"];
const EFFECTS: Record<DataKind, readonly string[]> = {
  decisions: [
    "learned",
    "answers",
    "showings",
    "comparisons",
    "vault_change",
    "pauses",
    "improve",
    "history",
  ],
  suggestions: ["comes_back", "titles", "item_pauses", "today"],
  calls: ["see_why", "last_call"],
};
const STAYS: Record<DataKind, readonly string[]> = {
  decisions: ["chats", "settings", "today"],
  suggestions: ["decisions", "chats", "settings"],
  calls: ["today"],
};

function dateTime(stamp: string): string {
  return `${localDate(stamp)} ${localClock(stamp)}`;
}

function shownAs(entry: "decision" | "report", level: DecisionLevel | null): string {
  if (entry === "report" || level === null) return t("decision_page.data.level.report");
  if (level === "silent") return t("decision_page.data.series.held");
  if (level === "list") return t("decision_page.data.series.listed");
  return t(`decision_page.data.level.${level}`);
}

function sinceStamp(period: string, nowMs: number): string | null {
  if (period === "any") return null;
  const day = new Date(nowMs);
  day.setHours(0, 0, 0, 0);
  day.setDate(day.getDate() - (period === "today" ? 0 : period === "7" ? 6 : 29));
  return day.toISOString();
}

function isBusy(error: unknown): boolean {
  return error instanceof DecisionLoopApiError && error.code === "busy";
}

function Select({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: readonly { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="dloop-data-filter">
      {label}
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option value={option.value} key={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function Evidence({ items }: { items: DataRecordView["evidence"] }) {
  return (
    <ul className="dloop-data-evidence">
      {items.map((item, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: This fixed read-only evidence list has no stable unique id.
        <li key={index}>
          {item.label}
          {item.ref ? ` · ${item.ref}` : null}
        </li>
      ))}
    </ul>
  );
}

function Details({ detail }: { detail: Detail }) {
  const lines: { label: string; value: string | number | null | undefined }[] = [];
  if ("call_id" in detail) {
    lines.push({ label: t("decision_page.data.detail.version"), value: detail.version });
    if (detail.outcome === "answered") {
      for (const question of detail.questions)
        lines.push({ label: question.label, value: question.answer });
    }
  } else if (detail.kind === "decisions") {
    lines.push(
      { label: t("decision_page.data.detail.at"), value: dateTime(detail.at) },
      { label: t("decision_page.data.topics.topic"), value: topicLabel(detail.topic) },
      { label: t("decision_page.data.filter.shown"), value: shownAs(detail.entry, detail.level) },
      {
        label: t("decision_page.data.detail.policy"),
        value: t(
          detail.policy === "model"
            ? "decision_page.data.policy.model"
            : "decision_page.data.policy.rules",
        ),
      },
      { label: t("decision_page.data.detail.why"), value: detail.why },
      {
        label: t("decision_page.data.filter.answer"),
        value: [
          detail.answer === null
            ? t("decision_page.data.answer.none")
            : t(`decision_page.data.answer.${detail.answer}`),
          detail.answer_reason
            ? (REASON_LABELS[detail.answer_reason as keyof typeof REASON_LABELS] ??
              detail.answer_reason)
            : null,
          detail.answered_at ? dateTime(detail.answered_at) : null,
        ]
          .filter(Boolean)
          .join(" · "),
      },
    );
    lines.push({ label: tf("decision_page.data.detail.shown", { n: detail.shown }), value: null });
    if (detail.opened_at)
      lines.push({
        label: t("decision_page.data.detail.opened"),
        value: dateTime(detail.opened_at),
      });
    if (detail.acted_at)
      lines.push({ label: t("decision_page.data.state.acted"), value: dateTime(detail.acted_at) });
    if (detail.tokens !== null && detail.cost_usd !== null)
      lines.push({
        label: tf("decision_page.data.detail.cost", {
          tokens: formatNumber(detail.tokens),
          cost: formatMoney(detail.cost_usd),
        }),
        value: null,
      });
  } else {
    lines.push(
      { label: t("decision_page.data.detail.created"), value: dateTime(detail.created_at) },
      { label: t("decision_page.data.detail.updated"), value: dateTime(detail.updated_at) },
      { label: t("decision_page.data.topics.topic"), value: topicLabel(detail.topic) },
      {
        label: t("decision_page.data.filter.state"),
        value: t(`decision_page.data.state.${detail.state}`),
      },
      { label: t("decision_page.data.filter.shown"), value: shownAs("decision", detail.level) },
      { label: t("decision_page.data.detail.what"), value: detail.what },
      { label: t("decision_page.data.detail.why"), value: detail.why },
    );
    if (detail.opened_at)
      lines.push({
        label: t("decision_page.data.detail.opened"),
        value: dateTime(detail.opened_at),
      });
  }
  return (
    <dl className="dloop-data-detail">
      {lines.map((line, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: Detail lines are a fixed read-only projection.
        <div key={index}>
          <dt>{line.label}</dt>
          <dd>{line.value}</dd>
        </div>
      ))}
      {"evidence" in detail ? (
        <div>
          <dt>{t("decision_page.data.detail.evidence")}</dt>
          <dd>
            <Evidence items={detail.evidence} />
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

export function DataRecords({
  api,
  asOf,
  kind,
  onKind,
  topic,
  onTopic,
  writable,
  onDeleted,
  onOpenContext,
  nowMs = Date.now(),
}: Props) {
  const [shown, setShown] = useState<DataShown | null>(null);
  const [answer, setAnswer] = useState<DataAnswer | null>(null);
  const [state, setState] = useState<(typeof ITEM_STATES)[number] | null>(null);
  const [outcome, setOutcome] = useState<(typeof OUTCOMES)[number] | null>(null);
  const [period, setPeriod] = useState("any");
  const [view, setView] = useState<DataRecordsView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [reload, setReload] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [selectionLimit, setSelectionLimit] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailError, setDetailError] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deletedLine, setDeletedLine] = useState("");
  const pageController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const firstNew = useRef<string | null>(null);

  const since = kind === "calls" ? null : sinceStamp(period, nowMs);
  const tooLate = since !== null && since >= asOf;
  const query = useMemo<DataQuery>(() => {
    const base = { ids: null, since, until: asOf, before: null };
    if (kind === "decisions")
      return {
        ...base,
        kind,
        topic: topic as DecisionTopic | null,
        shown,
        answer,
        state: null,
        outcome: null,
      };
    if (kind === "suggestions")
      return {
        ...base,
        kind,
        topic: topic as DecisionCandidate | null,
        shown: null,
        answer: null,
        state,
        outcome: null,
      };
    return {
      kind,
      ids: null,
      topic: null,
      shown: null,
      answer: null,
      state: null,
      outcome,
      since: null,
      until: null,
      before: null,
    };
  }, [kind, topic, shown, answer, state, outcome, since, asOf]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: reload explicitly retries the current query.
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    pageController.current?.abort();
    detailController.current?.abort();
    pageController.current = controller;
    setView(null);
    setSelected([]);
    setOpenId(null);
    setDetail(null);
    setLoadError(null);
    setLoading(!tooLate);
    if (!tooLate)
      api
        .dataRecords(query, controller.signal)
        .then((result) => {
          if (live && !controller.signal.aborted) setView(result);
        })
        .catch((error: unknown) => {
          if (live && !controller.signal.aborted) setLoadError(isBusy(error) ? "busy" : "failed");
        })
        .finally(() => {
          if (live && !controller.signal.aborted) setLoading(false);
        });
    return () => {
      live = false;
      controller.abort();
      pageController.current?.abort();
      detailController.current?.abort();
    };
  }, [api, query, reload, tooLate]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: view signals that the first new row mounted.
  useEffect(() => {
    if (!firstNew.current) return;
    const id = firstNew.current;
    const node = [...document.querySelectorAll<HTMLElement>("[data-data-row]")].find(
      (row) => row.dataset.dataRow === id,
    );
    if (node) {
      node.focus();
      firstNew.current = null;
    }
  }, [view]);

  function loadMore() {
    if (!view?.next || loading) return;
    const cursor = view.next;
    pageController.current?.abort();
    const controller = new AbortController();
    pageController.current = controller;
    setLoading(true);
    setLoadError(null);
    api
      .dataRecords({ ...query, before: cursor } as DataQuery, controller.signal)
      .then((page) => {
        if (controller.signal.aborted || page.kind !== kind) return;
        firstNew.current = page.rows[0]?.id ?? null;
        setView((old) =>
          old && old.kind === page.kind
            ? ({ ...page, rows: [...old.rows, ...page.rows] } as DataRecordsView)
            : old,
        );
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setLoadError(isBusy(error) ? "busy" : "failed");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  }

  function toggleDetail(id: string) {
    detailController.current?.abort();
    if (openId === id) {
      setOpenId(null);
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    detailController.current = controller;
    setOpenId(id);
    setDetail(null);
    setDetailError(false);
    setDetailLoading(true);
    const read =
      kind === "calls"
        ? api.call(id, controller.signal)
        : api.dataRecord(kind, id, controller.signal);
    read
      .then((result) => {
        if (!controller.signal.aborted) setDetail(result);
      })
      .catch(() => {
        if (!controller.signal.aborted) setDetailError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
  }

  function toggleSelected(id: string) {
    setSelected((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);
      if (current.length >= 50) {
        setSelectionLimit(true);
        return current;
      }
      setSelectionLimit(false);
      return [...current, id];
    });
  }

  const filtered =
    query.topic !== null ||
    query.shown !== null ||
    query.answer !== null ||
    query.state !== null ||
    query.since !== null;
  function openDelete(matching: boolean) {
    if (
      !writable ||
      !view ||
      (matching && (!filtered || kind === "calls")) ||
      (!matching && selected.length === 0)
    )
      return;
    const count = matching ? view.deletable : selected.length;
    const snapshot: DataQuery = matching
      ? { ...query, before: null }
      : ({
          kind,
          ids: [...selected],
          topic: null,
          shown: null,
          answer: null,
          state: null,
          outcome: null,
          since: null,
          until: null,
          before: null,
        } as DataQuery);
    setPendingDelete({ query: snapshot, count, matching, kind });
    setDeleteError(null);
  }

  async function confirmDelete() {
    if (!pendingDelete || deleting || !writable) return;
    const confirmed = pendingDelete;
    setDeleting(true);
    setDeleteError(null);
    try {
      const result = await api.deleteData(confirmed.query);
      const gone =
        confirmed.query.ids === null
          ? 0
          : Math.max(0, confirmed.query.ids.length - result.deleted - result.kept);
      setDeletedLine(
        [
          result.deleted && tf("decision_page.data.deleted", { n: result.deleted }),
          result.kept && tf("decision_page.data.deleted.kept", { n: result.kept }),
          gone && tf("decision_page.data.deleted.gone", { n: gone }),
        ]
          .filter(Boolean)
          .join(" "),
      );
      setPendingDelete(null);
      setSelected([]);
      setReload((value) => value + 1);
      onDeleted();
    } catch (error) {
      setDeleteError(
        isBusy(error) ? t("decision_page.data.busy") : t("decision_page.data.delete.failed"),
      );
    } finally {
      setDeleting(false);
    }
  }

  const sections: readonly ConfirmSection[] | undefined = pendingDelete
    ? [
        {
          heading: t("decision_page.data.confirm.changes"),
          items: EFFECTS[pendingDelete.kind].map((key) =>
            t(`decision_page.data.effect.${key}` as Parameters<typeof t>[0]),
          ),
        },
        {
          heading: t("decision_page.data.confirm.stays"),
          items: STAYS[pendingDelete.kind].map((key) =>
            t(`decision_page.data.stay.${key}` as Parameters<typeof t>[0]),
          ),
        },
      ]
    : undefined;

  return (
    <section className="dloop-data-records" aria-labelledby="data-browse">
      <h3 id="data-browse" tabIndex={-1}>
        {t("decision_page.data.browse.title")}
      </h3>
      <Choice
        label={t("decision_page.data.browse.kind")}
        options={DATA_KINDS.map((value) => ({
          value,
          label:
            value === "decisions"
              ? t("decision_page.data.browse.decisions")
              : t(`decision_page.data.kind.${value}.name`),
        }))}
        value={kind}
        onChange={onKind}
      />
      <div className="dloop-data-filters">
        {kind !== "calls" ? (
          <Select
            label={t("decision_page.data.topics.topic")}
            value={topic ?? ""}
            onChange={(value) => onTopic(value || null)}
            options={[
              { value: "", label: t("decision_page.data.topic.any") },
              ...(kind === "decisions" ? TOPICS : CANDIDATES).map((value) => ({
                value,
                label: topicLabel(value),
              })),
            ]}
          />
        ) : null}
        {kind === "decisions" ? (
          <>
            <Select
              label={t("decision_page.data.filter.shown")}
              value={shown ?? ""}
              onChange={(value) => setShown(value ? (value as DataShown) : null)}
              options={[
                { value: "", label: t("decision_page.data.filter.any") },
                ...DATA_SHOWN.map((value) => ({
                  value,
                  label:
                    value === "held"
                      ? t("decision_page.data.series.held")
                      : value === "listed"
                        ? t("decision_page.data.series.listed")
                        : value === "attention"
                          ? t("decision_page.data.series.attention")
                          : t("decision_page.data.level.report"),
                })),
              ]}
            />
            <Select
              label={t("decision_page.data.filter.answer")}
              value={answer ?? ""}
              onChange={(value) => setAnswer(value ? (value as DataAnswer) : null)}
              options={[
                { value: "", label: t("decision_page.data.filter.any") },
                ...ANSWERS.map((value) => ({
                  value,
                  label: t(`decision_page.data.answer.${value}`),
                })),
              ]}
            />
          </>
        ) : null}
        {kind === "suggestions" ? (
          <Select
            label={t("decision_page.data.filter.state")}
            value={state ?? ""}
            onChange={(value) => setState(value ? (value as (typeof ITEM_STATES)[number]) : null)}
            options={[
              { value: "", label: t("decision_page.data.filter.any") },
              ...ITEM_STATES.map((value) => ({
                value,
                label: t(`decision_page.data.state.${value}`),
              })),
            ]}
          />
        ) : null}
        {kind === "calls" ? (
          <Select
            label={t("decision_page.data.filter.outcome")}
            value={outcome ?? ""}
            onChange={(value) => setOutcome(value ? (value as (typeof OUTCOMES)[number]) : null)}
            options={[
              { value: "", label: t("decision_page.data.filter.any") },
              ...OUTCOMES.map((value) => ({
                value,
                label: t(`decision_page.data.outcome.${value}`),
              })),
            ]}
          />
        ) : null}
        {kind !== "calls" ? (
          <Select
            label={t("decision_page.data.filter.period")}
            value={period}
            onChange={setPeriod}
            options={[
              { value: "any", label: t("decision_page.data.period.any") },
              { value: "today", label: t("decision_page.data.period.today") },
              { value: "7", label: t("decision_page.data.period.7") },
              { value: "30", label: t("decision_page.data.range.30") },
            ]}
          />
        ) : null}
      </div>
      <p>{tf("decision_page.data.as_of", { time: dateTime(asOf) })}</p>
      {deletedLine ? <p role="status">{deletedLine}</p> : null}
      {tooLate ? (
        <p>{t("decision_page.data.results.none")}</p>
      ) : view ? (
        <p>
          {view.total === 0
            ? t("decision_page.data.results.none")
            : kind === "calls"
              ? tf("decision_page.data.results.calls", { total: view.total })
              : kind === "decisions"
                ? tf("decision_page.data.results.decisions", {
                    total: view.total,
                    deletable: view.deletable,
                  })
                : tf("decision_page.data.results.suggestions", {
                    total: view.total,
                    deletable: view.deletable,
                  })}
        </p>
      ) : null}
      {kind === "calls" ? (
        <div className="dloop-data-context">
          <p>{t("decision_page.data.calls.context")}</p>
          <button type="button" className="btn txt sm" onClick={onOpenContext}>
            {t("decision_page.data.open_context")}
          </button>
        </div>
      ) : null}
      {view ? (
        <ul className="dloop-data-list">
          {view.rows.map((row: Row) => {
            const rowKind = view.kind;
            const name =
              rowKind === "decisions"
                ? `${topicLabel((row as Extract<Row, { entry: string }>).topic)} · ${shownAs((row as Extract<Row, { entry: string }>).entry, (row as Extract<Row, { entry: string }>).level)} · ${dateTime(row.at)}`
                : (row as { title: string }).title || t("decision_page.data.unreadable");
            const kept = "kept" in row ? row.kept : null;
            return (
              <li key={row.id} data-data-row={row.id} tabIndex={-1}>
                <div className="dloop-data-row">
                  {kept === null ? (
                    <label>
                      <input
                        type="checkbox"
                        aria-label={tf("decision_page.data.select", { name })}
                        checked={selected.includes(row.id)}
                        onChange={() => toggleSelected(row.id)}
                      />
                      {t("decision_page.data.select").split("{")[0]}
                    </label>
                  ) : null}
                  <div>
                    <strong>{name}</strong>
                    {rowKind === "decisions" ? (
                      <span className="dloop-data-chip">
                        {(row as Extract<Row, { entry: string }>).answer === null
                          ? t("decision_page.data.answer.none")
                          : t(
                              `decision_page.data.answer.${(row as Extract<Row, { entry: string }>).answer}` as Parameters<
                                typeof t
                              >[0],
                            )}
                      </span>
                    ) : null}
                    {rowKind === "suggestions" ? (
                      <p>
                        {topicLabel((row as Extract<Row, { state: string }>).topic)} ·{" "}
                        {t(
                          `decision_page.data.state.${(row as Extract<Row, { state: string }>).state}`,
                        )}{" "}
                        · {dateTime(row.at)}
                      </p>
                    ) : null}
                    {rowKind === "calls" ? (
                      <p>
                        {(row as Extract<Row, { outcome: string }>).topic
                          ? topicLabel(
                              (row as Extract<Row, { outcome: string }>).topic as DecisionCandidate,
                            )
                          : ""}{" "}
                        ·{" "}
                        {t(
                          `decision_page.data.outcome.${(row as Extract<Row, { outcome: string }>).outcome}`,
                        )}{" "}
                        · {dateTime(row.at)} ·{" "}
                        {tf("decision_page.data.tokens", {
                          n: formatNumber((row as Extract<Row, { outcome: string }>).tokens),
                        })}{" "}
                        · {formatMoney((row as Extract<Row, { outcome: string }>).cost_usd)}
                      </p>
                    ) : null}
                    {kept ? <p>{t(`decision_page.data.kept.${kept}`)}</p> : null}
                  </div>
                  <button type="button" className="btn txt sm" onClick={() => toggleDetail(row.id)}>
                    {openId === row.id
                      ? t("decision_page.data.details.hide")
                      : t("decision_page.data.details")}
                  </button>
                </div>
                {openId === row.id ? (
                  detailLoading ? (
                    <Loading>{t("decision_page.data.loading")}</Loading>
                  ) : detailError ? (
                    <p role="alert">{t("decision_page.data.load.failed")}</p>
                  ) : detail ? (
                    <Details detail={detail} />
                  ) : null
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {view?.next ? (
        <button type="button" className="btn alt sm" onClick={loadMore} disabled={loading}>
          {t("decision_page.data.more")}
        </button>
      ) : null}
      {loading && !view ? <Loading>{t("decision_page.data.loading")}</Loading> : null}
      {loadError ? (
        <p role="alert">
          {loadError === "busy"
            ? t("decision_page.data.busy")
            : t("decision_page.data.load.failed")}{" "}
          <button
            type="button"
            className="btn txt sm"
            onClick={() => setReload((value) => value + 1)}
          >
            {t("common.button.retry")}
          </button>
        </p>
      ) : null}
      {selectionLimit ? <p role="alert">{t("decision_page.data.select.limit")}</p> : null}
      <div className="dloop-data-actions">
        <button
          type="button"
          className="btn alt sm"
          disabled={!writable || selected.length === 0}
          onClick={() => openDelete(false)}
        >
          {tf("decision_page.data.delete.selected", { n: selected.length })}
        </button>
        {kind !== "calls" ? (
          filtered ? (
            <button
              type="button"
              className="btn alt sm"
              disabled={!writable || !view || view.deletable === 0}
              onClick={() => openDelete(true)}
            >
              {tf("decision_page.data.delete.matching", { n: view?.deletable ?? 0 })}
            </button>
          ) : (
            <p>{t("decision_page.data.delete.need_filter")}</p>
          )
        ) : null}
      </div>
      {deleting && pendingDelete?.kind === "decisions" ? (
        <p role="status">{t("decision_page.data.removing")}</p>
      ) : null}
      {pendingDelete ? (
        <ConfirmDialog
          label={tf(
            pendingDelete.kind === "calls"
              ? "decision_page.data.confirm.calls"
              : pendingDelete.kind === "decisions"
                ? pendingDelete.matching
                  ? "decision_page.data.confirm.decisions_matching"
                  : "decision_page.data.confirm.decisions"
                : pendingDelete.matching
                  ? "decision_page.data.confirm.suggestions_matching"
                  : "decision_page.data.confirm.suggestions",
            { n: pendingDelete.count },
          )}
          body={t(`decision_page.data.confirm.body.${pendingDelete.kind}`)}
          sections={sections}
          confirmLabel={t("decision_page.data.confirm.delete")}
          destructive
          irreversible
          busy={deleting || !writable}
          error={deleteError}
          onConfirm={confirmDelete}
          onCancel={() => {
            if (!deleting) setPendingDelete(null);
          }}
        />
      ) : null}
    </section>
  );
}
