import { useId, useState } from "react";
import type { DataDay, DataTopic } from "../../../clients/decisionLoop/types";
import { t, tf } from "../../../i18n/t";
import { candidateLabel, formatNumber } from "../wording";
import "./DataParts.css";

export type DataRange = 30 | 90 | 365;

type ActivityColumn = {
  label: string;
  held: number;
  listed: number;
  attention: number;
  reports: number;
};

const ACTIVITY_SERIES = [
  ["held", "decision_page.data.series.held", "dloop-held"],
  ["listed", "decision_page.data.series.listed", "dloop-listed"],
  ["attention", "decision_page.data.series.attention", "dloop-attention"],
  ["reports", "decision_page.data.series.reports", "dloop-reports"],
] as const;

const ANSWER_SERIES = [
  ["useful", "decision_page.data.answer.useful", "dloop-useful"],
  ["wrong", "decision_page.data.answer.wrong", "dloop-wrong"],
  ["later", "decision_page.data.answer.later", "dloop-later"],
  ["mute", "decision_page.data.answer.mute", "dloop-stopped"],
] as const;

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function validDay(day: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const date = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, date));
  return parsed.getUTCFullYear() === year &&
    parsed.getUTCMonth() === month - 1 &&
    parsed.getUTCDate() === date
    ? parsed
    : null;
}

export function localDate(stamp: string): string {
  const date = new Date(stamp);
  return Number.isNaN(date.getTime())
    ? ""
    : `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

export function shiftDay(day: string, delta: number): string {
  const date = validDay(day);
  if (!date || !Number.isInteger(delta)) return "";
  date.setUTCDate(date.getUTCDate() + delta);
  return `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`;
}

export function topicLabel(topic: string): string {
  return topic === "unwatched" ? t("decision_page.data.topic.unwatched") : candidateLabel(topic);
}

function rangeLabel(range: DataRange): string {
  if (range === 30) return t("decision_page.data.range.30");
  if (range === 90) return t("decision_page.data.range.90");
  return t("decision_page.data.range.365");
}

export interface InventoryCardProps {
  name: string;
  meaning: string;
  count: number;
  dates: string;
  limit: string;
  note: string;
  meters?: readonly { value: number; max: number; label: string }[];
  actions?: readonly { label: string; onClick: () => void }[];
}

export function InventoryCard({
  name,
  meaning,
  count,
  dates,
  limit,
  note,
  meters = [],
  actions = [],
}: InventoryCardProps) {
  const headingId = useId();
  return (
    <article className="dloop-card" aria-labelledby={headingId}>
      <h4 id={headingId}>{name}</h4>
      <p className="dloop-card-meaning">{meaning}</p>
      <p className="dloop-card-count">{formatNumber(count)}</p>
      <p className="dloop-card-detail">{dates}</p>
      <p className="dloop-card-detail">{limit}</p>
      {meters.length > 0 ? (
        <div className="dloop-card-meters">
          {meters.map((meter) => (
            // biome-ignore lint/a11y/useSemanticElements: Data meters need a styled span with visible text.
            <span
              className="dloop-meter"
              role="meter"
              aria-label={meter.label}
              aria-valuemin={0}
              aria-valuemax={meter.max}
              aria-valuenow={meter.value}
              key={meter.label}
            >
              {tf("decision_page.data.meter", {
                value: formatNumber(meter.value),
                max: formatNumber(meter.max),
              })}
            </span>
          ))}
        </div>
      ) : null}
      <p className="dloop-card-note">{note}</p>
      {actions.length > 0 ? (
        <div className="dloop-card-actions">
          {actions.map((action) => (
            <button
              className="btn alt sm"
              type="button"
              onClick={action.onClick}
              key={action.label}
            >
              {action.label}
            </button>
          ))}
        </div>
      ) : null}
    </article>
  );
}

function dayMap(days: readonly DataDay[]): Map<string, DataDay> {
  return new Map(days.map((day) => [day.day, day]));
}

function activityColumns(
  days: readonly DataDay[],
  today: string,
  range: DataRange,
): ActivityColumn[] {
  const firstDay = shiftDay(today, -(range - 1));
  const byDay = dayMap(days);
  const columns: ActivityColumn[] = [];
  if (range !== 365) {
    for (let offset = 0; offset < range; offset++) {
      const day = shiftDay(firstDay, offset);
      const value = byDay.get(day);
      columns.push({
        label: day,
        held: value?.held ?? 0,
        listed: value?.listed ?? 0,
        attention: value?.attention ?? 0,
        reports: value?.reports ?? 0,
      });
    }
    return columns;
  }

  const firstWeekLength = range % 7 || 7;
  for (let startOffset = 0; startOffset < range; ) {
    const weekLength = startOffset === 0 ? firstWeekLength : 7;
    const endOffset = Math.min(range - 1, startOffset + weekLength - 1);
    const sum: ActivityColumn = {
      label: shiftDay(firstDay, endOffset),
      held: 0,
      listed: 0,
      attention: 0,
      reports: 0,
    };
    for (let offset = startOffset; offset <= endOffset; offset++) {
      const value = byDay.get(shiftDay(firstDay, offset));
      if (!value) continue;
      sum.held += value.held;
      sum.listed += value.listed;
      sum.attention += value.attention;
      sum.reports += value.reports;
    }
    columns.push(sum);
    startOffset = endOffset + 1;
  }
  return columns;
}

function hasActivity(columns: readonly ActivityColumn[]): boolean {
  return columns.some(
    (column) => column.held + column.listed + column.attention + column.reports > 0,
  );
}

function seriesTotals(columns: readonly ActivityColumn[]) {
  return Object.fromEntries(
    ACTIVITY_SERIES.map(([key]) => [key, columns.reduce((sum, column) => sum + column[key], 0)]),
  ) as Record<(typeof ACTIVITY_SERIES)[number][0], number>;
}

function ActivityTable({
  columns,
  weekly,
}: {
  columns: readonly ActivityColumn[];
  weekly: boolean;
}) {
  return (
    <table className="dloop-data-table">
      <caption>{t("decision_page.data.activity.title")}</caption>
      <thead>
        <tr>
          <th scope="col">
            {t(weekly ? "decision_page.data.table.week" : "decision_page.data.table.day")}
          </th>
          {ACTIVITY_SERIES.map(([, key]) => (
            <th scope="col" key={key}>
              {t(key)}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {columns.map((column) => (
          <tr key={column.label}>
            <th scope="row">{column.label}</th>
            {ACTIVITY_SERIES.map(([key]) => (
              <td key={key}>{formatNumber(column[key])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ActivityChart({
  days,
  today,
  range,
}: {
  days: readonly DataDay[];
  today: string;
  range: DataRange;
}) {
  const [showTable, setShowTable] = useState(false);
  const tableId = useId();
  const columns = activityColumns(days, today, range);
  const totals = seriesTotals(columns);
  const totalDecisions = totals.held + totals.listed + totals.attention;
  const hasRows = hasActivity(columns);
  const weekly = range === 365;
  const summary = tf("decision_page.data.activity.summary", {
    range: rangeLabel(range),
    decisions: formatNumber(totalDecisions),
    held: formatNumber(totals.held),
    listed: formatNumber(totals.listed),
    attention: formatNumber(totals.attention),
    reports: formatNumber(totals.reports),
  });
  const max = Math.max(
    1,
    ...columns.map((column) => column.held + column.listed + column.attention + column.reports),
  );
  const width = columns.length * 4;
  const height = 100;

  return (
    <div className="dloop-chart">
      {hasRows ? (
        <>
          <svg
            className="dloop-activity-svg"
            role="img"
            aria-label={summary}
            viewBox={`0 0 ${width} ${height}`}
            preserveAspectRatio="none"
          >
            {columns.map((column, index) => {
              const values = ACTIVITY_SERIES.map(([key, , className]) => ({
                value: column[key],
                className,
              })).filter((series) => series.value > 0);
              const scale = (height - Math.max(0, values.length - 1)) / max;
              let bottom = height;
              return (
                <g key={column.label}>
                  {values.map((series) => {
                    const segmentHeight = series.value * scale;
                    bottom -= segmentHeight;
                    const y = bottom;
                    bottom -= 1;
                    return (
                      <rect
                        key={series.className}
                        className={series.className}
                        x={index * 4}
                        y={y}
                        width={3}
                        height={segmentHeight}
                      />
                    );
                  })}
                </g>
              );
            })}
          </svg>
          <p className="dloop-data-summary">{summary}</p>
          <ul className="dloop-chart-legend">
            {ACTIVITY_SERIES.map(([key, label, className]) => (
              <li key={key}>
                <span className={`dloop-legend-mark ${className}`} aria-hidden="true" />
                {t(label)}: {formatNumber(totals[key])}
              </li>
            ))}
          </ul>
          <p className="dloop-data-hint">
            {t(weekly ? "decision_page.data.activity.weeks" : "decision_page.data.activity.days")}
          </p>
        </>
      ) : (
        <p className="dloop-data-empty">{t("decision_page.data.empty_range")}</p>
      )}
      <button
        type="button"
        className="btn txt sm"
        aria-expanded={showTable}
        aria-controls={tableId}
        onClick={() => setShowTable((shown) => !shown)}
      >
        {t(showTable ? "decision_page.data.table.hide" : "decision_page.data.table.show")}
      </button>
      {showTable ? (
        <div id={tableId} className="dloop-table-wrap">
          <ActivityTable columns={hasRows ? columns : []} weekly={weekly} />
        </div>
      ) : null}
    </div>
  );
}

type AnswerSegment = { label: string; value: number; percent: number; className: string };

function percentages(values: readonly number[]): number[] {
  const total = values.reduce((sum, value) => sum + value, 0);
  if (total === 0) return values.map(() => 0);
  const exact = values.map((value) => (value * 100) / total);
  const result = exact.map(Math.floor);
  const remaining = 100 - result.reduce((sum, value) => sum + value, 0);
  const order = exact
    .map((value, index) => ({ index, fraction: value - Math.floor(value) }))
    .sort((a, b) => b.fraction - a.fraction);
  for (let i = 0; i < remaining; i++) {
    const index = order[i]?.index;
    if (index !== undefined) result[index] = (result[index] ?? 0) + 1;
  }
  return result;
}

function shareText(segment: AnswerSegment): string {
  return tf("decision_page.data.answers.share", {
    label: segment.label,
    n: formatNumber(segment.value),
    percent: segment.percent,
  });
}

function AnswerBar({ title, segments }: { title: string; segments: readonly AnswerSegment[] }) {
  const accessibleLabel = segments.map(shareText).join(". ");
  return (
    <section className="dloop-answer-group">
      <h4>{title}</h4>
      <div className="dloop-answer-visual" role="img" aria-label={accessibleLabel}>
        {segments.map((segment) => (
          <span
            className={segment.className}
            style={{ width: `${segment.percent}%` }}
            aria-hidden="true"
            key={segment.label}
          />
        ))}
      </div>
      <ul className="dloop-answer-legend">
        {segments.map((segment) => (
          <li key={segment.label}>{shareText(segment)}</li>
        ))}
      </ul>
    </section>
  );
}

export function AnswerBars({
  days,
  today,
  range,
}: {
  days: readonly DataDay[];
  today: string;
  range: DataRange;
}) {
  const start = shiftDay(today, -(range - 1));
  const windowDays = days.filter((day) => day.day >= start && day.day <= today);
  const totals = windowDays.reduce(
    (sum, day) => ({
      shown: sum.shown + day.listed + day.attention,
      held: sum.held + day.held,
      reports: sum.reports + day.reports,
      useful: sum.useful + day.useful,
      wrong: sum.wrong + day.wrong,
      later: sum.later + day.later,
      mute: sum.mute + day.mute,
      rightToHold: sum.rightToHold + day.right_to_hold,
      missed: sum.missed + day.missed,
    }),
    {
      shown: 0,
      held: 0,
      reports: 0,
      useful: 0,
      wrong: 0,
      later: 0,
      mute: 0,
      rightToHold: 0,
      missed: 0,
    },
  );
  const reportsLine = (
    <p className="dloop-data-hint">
      {tf("decision_page.data.answers.reports", { n: formatNumber(totals.reports) })}
    </p>
  );
  if (totals.shown === 0 && totals.held === 0) {
    return (
      <>
        <p className="dloop-data-empty">{t("decision_page.data.empty_range")}</p>
        {reportsLine}
      </>
    );
  }

  const shownValues = [
    totals.useful,
    totals.wrong,
    totals.later,
    totals.mute,
    Math.max(0, totals.shown - totals.useful - totals.wrong - totals.later - totals.mute),
  ];
  const heldValues = [
    totals.rightToHold,
    totals.missed,
    Math.max(0, totals.held - totals.rightToHold - totals.missed),
  ];
  const shownSeries: AnswerSegment[] = [
    ...ANSWER_SERIES.map(([, label, className], index) => ({
      label: t(label),
      value: shownValues[index] ?? 0,
      percent: 0,
      className,
    })),
    {
      label: t("decision_page.data.answer.none"),
      value: shownValues[4] ?? 0,
      percent: 0,
      className: "dloop-no-answer",
    },
  ];
  const heldSeries: AnswerSegment[] = [
    {
      label: t("decision_page.data.answer.right_to_hold"),
      value: heldValues[0] ?? 0,
      percent: 0,
      className: "dloop-useful",
    },
    {
      label: t("decision_page.data.answer.missed"),
      value: heldValues[1] ?? 0,
      percent: 0,
      className: "dloop-wrong",
    },
    {
      label: t("decision_page.data.answer.none"),
      value: heldValues[2] ?? 0,
      percent: 0,
      className: "dloop-no-answer",
    },
  ];
  const shownPercentages = percentages(shownValues);
  const heldPercentages = percentages(heldValues);
  shownSeries.forEach((segment, index) => {
    segment.percent = shownPercentages[index] ?? 0;
  });
  heldSeries.forEach((segment, index) => {
    segment.percent = heldPercentages[index] ?? 0;
  });

  return (
    <div className="dloop-answer-bars">
      <AnswerBar
        title={tf("decision_page.data.answers.shown", { n: formatNumber(totals.shown) })}
        segments={shownSeries}
      />
      <AnswerBar
        title={tf("decision_page.data.answers.held", { n: formatNumber(totals.held) })}
        segments={heldSeries}
      />
      {reportsLine}
    </div>
  );
}

function TopicBar({ topic, busiest }: { topic: DataTopic; busiest: number }) {
  return (
    <span className="dloop-topic-track" aria-hidden="true">
      <span className="dloop-held" style={{ width: `${(topic.held / busiest) * 100}%` }} />
      <span className="dloop-listed" style={{ width: `${(topic.shown / busiest) * 100}%` }} />
      <span className="dloop-reports" style={{ width: `${(topic.reports / busiest) * 100}%` }} />
    </span>
  );
}

export function TopicTable({
  topics,
  onTopic,
}: {
  topics: readonly DataTopic[];
  onTopic: (topic: string) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? topics : topics.slice(0, 8);
  const busiest = Math.max(1, ...topics.map((topic) => topic.held + topic.shown + topic.reports));
  const hidden = topics.length - visible.length;
  return (
    <div className="dloop-topics">
      <table className="dloop-data-table">
        <caption>{t("decision_page.data.topics.title")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("decision_page.data.topics.topic")}</th>
            <th scope="col">{t("decision_page.data.series.held")}</th>
            <th scope="col">{t("decision_page.data.topics.shown")}</th>
            <th scope="col">{t("decision_page.data.series.reports")}</th>
            <th scope="col">{t("decision_page.data.answer.useful")}</th>
            <th scope="col">{t("decision_page.data.answer.wrong")}</th>
            <th scope="col" aria-label={t("decision_page.data.topics.title")} />
          </tr>
        </thead>
        <tbody>
          {visible.map((topic) => (
            <tr key={topic.topic}>
              <th scope="row">
                <button className="btn txt sm" type="button" onClick={() => onTopic(topic.topic)}>
                  {topicLabel(topic.topic)}
                </button>
              </th>
              <td>{formatNumber(topic.held)}</td>
              <td>{formatNumber(topic.shown)}</td>
              <td>{formatNumber(topic.reports)}</td>
              <td>{formatNumber(topic.useful)}</td>
              <td>{formatNumber(topic.wrong)}</td>
              <td>
                <TopicBar topic={topic} busiest={busiest} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {hidden > 0 ? (
        <button className="btn txt sm" type="button" onClick={() => setShowAll(true)}>
          {tf("decision_page.data.topics.show_all", { n: hidden })}
        </button>
      ) : null}
    </div>
  );
}
