/**
 * The Learning row's read-only content: what changed since the start, every learned number in one
 * table, how well VIKI's p matches the user, and each playbook question's own useful rates, in
 * four props-only components, none with a disclosure, switch or state of their own. The fifth,
 * `ShadowPart`, shows the shadow version's agreement.
 * `LearningRows.tsx` composes `WhatChanged`, `AllNumbers`, `CalibrationPart` and `QuestionsPart`
 * into the Learning row, alongside that row's status, switch and Reset control;
 * `rows/SetupRows.tsx` `ModelRow` shows `ShadowPart` under Advanced: version and comparison.
 * This file only renders the numbers, from the wire's `LearningView`
 * (`../../../clients/decisionLoop/types`).
 */
import type {
  DecisionLevel,
  LearningCalibration,
  LearningQuestionRate,
  LearningShadowStats,
  LearningView,
} from "../../../clients/decisionLoop/types";
import { candidateLabel } from "../wording";

/** Not applicable: the Interruption bar's own Start and Based-on cells, and a null calibration or
 * question rate — the same character the bin ranges use ("0.2–0.4"). */
const DASH = "–";

/** `n` with thousands separators, pinned to en-US so a test's locale never moves the digits
 * (matches `DoingRows.tsx`'s own `formatNumber`). */
function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

/** A 0..1 rate as a whole-number percentage ("68%"). */
function formatPercent(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

/** `brier` rounded for display, or the dash while there is nothing to show yet. */
function formatBrier(brier: number | null): string {
  return brier !== null ? formatNumber(brier) : DASH;
}

/** A short calendar date ("Sep 1, 2026"), fixed to en-US and UTC so it never moves with the
 * host's own locale or timezone. Shared by `WhatChanged`'s "Updated <date>" (once a day has
 * passed) and `LearningRows.tsx`'s own "Learning since <date>" line, so the Learning row's two
 * dates never drift into two styles. */
export function calendarDate(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** The display word for a reach or attention-cost level, matching `DoingRows.tsx`'s own
 * `LEVEL_WORDS` (reach and costs never carry "silent", so this map leaves it out). */
const LEVEL_WORDS: Readonly<Partial<Record<DecisionLevel, string>>> = {
  list: "Listed",
  badge: "Badge",
  nudge: "Nudge",
  alert: "Alert",
};

function levelWord(level: DecisionLevel): string {
  return LEVEL_WORDS[level] ?? level;
}

/** One learned number: its own label, start and learned value, and the labelled support
 * behind it. */
interface LearnedNumber {
  key: string;
  label: string;
  start: number;
  now: number;
  support: number;
}

/** Every number the Learning row shows, in the wire's own order (bias, weights, topics, reach,
 * costs). The shared bias and the five question weights are fit together
 * from every labelled row at once, so both read their support from `learning.labels` rather than
 * a count of their own. */
function learnedNumbers(learning: LearningView): LearnedNumber[] {
  return [
    {
      key: "bias",
      label: "Baseline",
      start: learning.bias.start,
      now: learning.bias.now,
      support: learning.labels,
    },
    ...learning.weights.map((weight) => ({
      key: `weight.${weight.name}`,
      label: weight.label,
      start: weight.start,
      now: weight.now,
      support: learning.labels,
    })),
    ...learning.topics.map((topic) => ({
      key: `topic.${topic.candidate}`,
      label: `About ${candidateLabel(topic.candidate)}`,
      start: topic.start,
      now: topic.now,
      support: topic.support,
    })),
    ...learning.reach.map((reach) => ({
      key: `reach.${reach.level}`,
      label: `Seen in time at ${levelWord(reach.level)}`,
      start: reach.start,
      now: reach.now,
      support: reach.support,
    })),
    ...learning.costs.map((cost) => ({
      key: `cost.${cost.level}`,
      label: `Interruption cost at ${levelWord(cost.level)}`,
      start: cost.start,
      now: cost.now,
      support: cost.support,
    })),
  ];
}

export interface WhatChangedProps {
  learning: LearningView;
  nowMs: number;
}

/** A number counts as moved once it is at least this far from its start. */
const MOVED_THRESHOLD = 0.05;
/** A tiny slack against floating-point rounding, so a difference the user's numbers intend to
 * be exactly `MOVED_THRESHOLD` (e.g. 0.55 fit minus a 0.5 start) is never missed by a hair. */
const MOVED_EPSILON = 1e-9;
/** At most this many movers are shown, largest first. */
const MOVED_LIMIT = 5;

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** "Updated <time>": "<N> minutes ago" / "<N> hours ago" (full words, unlike `wording.ts`'s own
 * abbreviated `relativeTime`, tuned for the launcher's tighter space) within a day of `nowMs`,
 * else `calendarDate` — the same style as "Learning since <date>" — once a day or more has
 * passed, so a fit from last month never claims to be a count of hours. */
function updatedAt(fittedAt: string, nowMs: number): string {
  const at = Date.parse(fittedAt);
  if (Number.isNaN(at)) return "";
  const diff = Math.max(0, nowMs - at);
  if (diff < MINUTE_MS) return "just now";
  if (diff < HOUR_MS) {
    const minutes = Math.floor(diff / MINUTE_MS);
    return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
  }
  if (diff < DAY_MS) {
    const hours = Math.floor(diff / HOUR_MS);
    return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
  }
  return calendarDate(fittedAt);
}

/** "What changed": up to 5 numbers that moved at least 0.05 from their start, largest first,
 * or the no-movement sentence; "Updated <time>" underneath once VIKI has fitted at least once. */
export function WhatChanged({ learning, nowMs }: WhatChangedProps) {
  const fittedAt = learning.fitted_at;
  const moved = learnedNumbers(learning)
    .map((row) => ({ ...row, delta: row.now - row.start }))
    .filter((row) => Math.abs(row.delta) >= MOVED_THRESHOLD - MOVED_EPSILON)
    .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta))
    .slice(0, MOVED_LIMIT);
  return (
    <>
      {moved.length > 0 ? (
        <ul>
          {moved.map((row) => (
            <li key={row.key}>
              {`${row.label} — ${row.delta > 0 ? "higher" : "lower"} (${row.start.toFixed(2)} → ${row.now.toFixed(2)})`}
            </li>
          ))}
        </ul>
      ) : (
        <p className="dloop-hint">No large changes to VIKI's learned values yet.</p>
      )}
      {fittedAt !== null ? (
        <p className="dloop-hint">{`Updated ${updatedAt(fittedAt, nowMs)}`}</p>
      ) : null}
    </>
  );
}

export interface AllNumbersProps {
  learning: LearningView;
}

/** "Show all numbers": one table of every learned number plus the Interruption bar
 * (`attention_price`, read as "x of 3" — the pacing price has no start value or support count of
 * its own). */
export function AllNumbers({ learning }: AllNumbersProps) {
  return (
    <table id="learning-numbers" className="dloop-numbers">
      <thead>
        <tr>
          <th>Number</th>
          <th>Start</th>
          <th>Now</th>
          <th>Based on</th>
        </tr>
      </thead>
      <tbody>
        {learnedNumbers(learning).map((row) => (
          <tr key={row.key}>
            <td>{row.label}</td>
            <td>{formatNumber(row.start)}</td>
            <td>{formatNumber(row.now)}</td>
            <td>{formatNumber(row.support)}</td>
          </tr>
        ))}
        <tr>
          <td>Interruption bar</td>
          <td>{DASH}</td>
          <td>{`${formatNumber(learning.attention_price)} of 3`}</td>
          <td>{DASH}</td>
        </tr>
      </tbody>
    </table>
  );
}

export interface CalibrationPartProps {
  calibration: LearningCalibration;
}

/** "How well it matches you": the error score, then each non-empty bin's useful rate; "No
 * feedback results yet." before the first label. */
export function CalibrationPart({ calibration }: CalibrationPartProps) {
  if (calibration.n === 0) {
    return <p className="dloop-hint">No feedback results yet.</p>;
  }
  const summary = `Prediction error ${formatBrier(calibration.brier)} across ${formatNumber(calibration.n)} decisions with feedback. Lower is better; 0.25 is a coin-flip prediction.`;
  const bins = calibration.bins.filter((bin) => bin.n > 0);
  return (
    <>
      <p>{summary}</p>
      {bins.length > 0 ? (
        <ul>
          {bins.map((bin) => (
            <li key={`${bin.low}-${bin.high}`}>
              {`Predicted usefulness ${formatNumber(bin.low)}–${formatNumber(bin.high)}: ${
                bin.rate !== null ? formatPercent(bin.rate) : DASH
              } counted useful (${formatNumber(bin.n)} decisions with feedback)`}
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

export interface QuestionsPartProps {
  questions: LearningQuestionRate[];
}

/** "Each question": every playbook question's own useful rate when it said yes and when no; a
 * dash where there is not yet a rate for that side. */
export function QuestionsPart({ questions }: QuestionsPartProps) {
  return (
    <ul>
      {questions.map((question) => {
        const yes = question.yes_rate !== null ? formatPercent(question.yes_rate) : DASH;
        const no = question.no_rate !== null ? formatPercent(question.no_rate) : DASH;
        return (
          <li key={question.name}>
            {`${question.label} When the model answered Yes: ${yes} counted useful (${formatNumber(question.yes_n)} decisions with feedback) · When the model answered No: ${no} counted useful (${formatNumber(question.no_n)} decisions with feedback)`}
          </li>
        );
      })}
    </ul>
  );
}

export interface ShadowPartProps {
  shadow: LearningShadowStats;
}

/** "Try another version in the shadow": nothing while shadowing is off; "No decisions in the
 * shadow yet." before the first one; otherwise the agreement sentence, plus, once some of those
 * decisions are labelled, how its error compares to the version in use. */
export function ShadowPart({ shadow }: ShadowPartProps) {
  if (shadow.setting === "off") {
    return null;
  }
  if (shadow.decisions === 0) {
    return <p className="dloop-hint">No comparison decisions yet.</p>;
  }
  const agreement = `Same attention level as the version in use on ${formatNumber(shadow.agree)} of ${formatNumber(shadow.decisions)} decisions; same item type on ${formatNumber(shadow.kind_agree)}.`;
  const labelled =
    shadow.labels > 0 && shadow.brier !== null && shadow.live_brier !== null
      ? `Across ${formatNumber(shadow.labels)} decisions with feedback: prediction error ${formatNumber(shadow.brier)} for the comparison version, ${formatNumber(shadow.live_brier)} for the version in use.`
      : null;
  return (
    <>
      <p>{agreement}</p>
      {labelled !== null ? <p>{labelled}</p> : null}
    </>
  );
}
