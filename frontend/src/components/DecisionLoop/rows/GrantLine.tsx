/**
 * One line of "What VIKI may do": a kind of work, an Off / Ask first / Allowed choice (a line
 * with no `grant` shows its label as plain text instead), its hint, a daily limit with today's
 * count for each limit it has, optional notes, nested controls and a "More about this" disclosure.
 * The row decides what each line says and saves; this only draws one line.
 */

import { type ReactNode, useId } from "react";
import { t } from "../../../i18n/t";
import { Choice, type ChoiceOption } from "../DecisionRow";
import "./GrantLine.css";

export type Grant = "off" | "ask" | "allowed";

export interface GrantLimit {
  key: string;
  label: string;
  value: number;
  /** The "Today: N of M …" line under the limit. */
  today: string;
}

export interface GrantLineProps {
  label: string;
  hint: string;
  grant?: Grant;
  onGrant?: (grant: Grant) => void;
  limits: readonly GrantLimit[];
  onLimit: (key: string, value: number) => void;
  notes?: readonly string[];
  more?: ReactNode;
  disabled: boolean;
  children?: ReactNode;
}

const GRANT_OPTIONS: readonly ChoiceOption<Grant>[] = [
  { value: "off", label: "Off" },
  { value: "ask", label: "Ask first" },
  { value: "allowed", label: "Allowed" },
];

const LIMIT_OPTIONS: readonly number[] = Array.from({ length: 11 }, (_, n) => n);

export function GrantLine({
  label,
  hint,
  grant,
  onGrant,
  limits,
  onLimit,
  notes,
  more,
  disabled,
  children,
}: GrantLineProps) {
  const uid = useId();
  const hintId = `${uid}-hint`;
  const noteIds = (notes ?? []).map((_, index) => `${uid}-note-${index}`);
  return (
    <div className="dloop-grant">
      {grant ? (
        <Choice
          label={label}
          options={GRANT_OPTIONS}
          value={grant}
          describedBy={[hintId, ...noteIds].join(" ")}
          disabled={disabled}
          onChange={(next) => {
            if (disabled) return;
            onGrant?.(next);
          }}
        />
      ) : (
        <p className="dloop-grant-label">{label}</p>
      )}
      <p className="dloop-hint" id={hintId}>
        {hint}
      </p>
      {limits.map((limit) => (
        <div className="dloop-field" key={limit.key}>
          <label htmlFor={`${uid}-${limit.key}`}>{limit.label}</label>
          <select
            id={`${uid}-${limit.key}`}
            value={limit.value}
            disabled={disabled}
            onChange={(event) => {
              if (disabled) return;
              onLimit(limit.key, Number(event.target.value));
            }}
          >
            {LIMIT_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <p className="dloop-hint">{limit.today}</p>
        </div>
      ))}
      {(notes ?? []).map((note, index) => (
        <p className="dloop-hint dloop-grant-note" id={noteIds[index]} key={note}>
          {note}
        </p>
      ))}
      {children ? <div className="dloop-grant-nested">{children}</div> : null}
      {more ? (
        <details className="dloop-details">
          <summary>{t("decision_page.do.more")}</summary>
          {more}
        </details>
      ) : null}
    </div>
  );
}
