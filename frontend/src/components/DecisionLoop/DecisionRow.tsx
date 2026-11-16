/**
 * The Decision page's row kit: one
 * disclosure row plus the two controls its body most often needs, plus the controlled-open
 * pattern every row file builds its own disclosure state from. The row files (`rows/*.tsx`)
 * build every row — Decision model, What leaves this computer, and the rest —
 * from these exports; none of them re-implements a disclosure, a switch, a radio group or the
 * controlled/uncontrolled open split on its own. The markup and the `dloop-` styles in
 * `DecisionLoopPage.css` port Messaging's row pattern (`MessagingView.tsx`'s per-provider
 * `<section>`, `MessagingView.css`'s `.messaging-bot`/`.messaging-disclosure`/`.messaging-toggle`)
 * rather than importing it, so this page can diverge later without disturbing Messaging.
 */

import { type ReactNode, type Ref, useId, useState } from "react";

export interface DecisionRowProps {
  /** Stable, page-unique id: the row derives its heading, status and body ids from it, so a
   * caller that needs to point at this row's body (`aria-controls`, `aria-describedby`) can
   * always rebuild `${id}-body` itself without a further prop. */
  id: string;
  /** The row's name, shown in its disclosure button and read as the section's and the switch's
   * accessible name. */
  name: string;
  description?: string;
  status: ReactNode;
  /** The heading row's trailing control — typically a `Switch`, sometimes a plain button. */
  control: ReactNode;
  open: boolean;
  onToggle: () => void;
  /** The row's body. Always rendered (never unmounted while closed), only hidden, so a field
   * with state of its own in the body does not lose it when the row collapses. */
  children: ReactNode;
}

/** One Decision-page row: a section named by its own `<h3>` disclosure button
 * (`aria-expanded`/`aria-controls`), a `role="status"` chip, an optional trailing control, and a
 * body that stays mounted and is only toggled by the `hidden` attribute. */
export function DecisionRow({
  id,
  name,
  description,
  status,
  control,
  open,
  onToggle,
  children,
}: DecisionRowProps) {
  const headingId = `${id}-heading`;
  const bodyId = `${id}-body`;
  return (
    <section className="dloop-row" aria-labelledby={headingId}>
      <div className="dloop-row-heading">
        <h3 id={headingId} className="dloop-row-name">
          <button
            type="button"
            className="dloop-disclosure"
            aria-expanded={open}
            aria-controls={bodyId}
            aria-describedby={description ? `${id}-description` : undefined}
            onClick={onToggle}
          >
            <svg className="ico" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
              <path d="M4 6l4 4 4-4" />
            </svg>
            {name}
          </button>
        </h3>
        <span role="status" className="chip dloop-status">
          {status}
        </span>
        {control}
      </div>
      {description && (
        <p className="dloop-row-description" id={`${id}-description`}>
          {description}
        </p>
      )}
      <div id={bodyId} className="dloop-row-body" hidden={!open}>
        {children}
      </div>
    </section>
  );
}

export interface SwitchProps {
  checked: boolean;
  /** The id of the element this switch is named after — normally its row's heading id, so the
   * switch and the row share one accessible name (Messaging's `.messaging-toggle` does the
   * same). */
  labelledBy: string;
  describedBy?: string;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  /** Reaches the switch's button, so a caller can move focus to it. */
  ref?: Ref<HTMLButtonElement>;
}

/** An on/off switch styled as a track and knob, matching `MessagingView.tsx`'s own toggle: a
 * plain button rather than a checkbox input, so its whole visible state — track, knob and the
 * "On"/"Off" word — is drawn by the component instead of browser checkbox chrome. */
export function Switch({ checked, labelledBy, describedBy, onChange, disabled, ref }: SwitchProps) {
  return (
    <button
      ref={ref}
      type="button"
      role="switch"
      className="dloop-switch"
      aria-checked={checked}
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      aria-disabled={disabled || undefined}
      onClick={() => {
        if (!disabled) onChange(!checked);
      }}
    >
      <span className="dloop-switch-track" aria-hidden="true" />
      <span className="dloop-switch-state" aria-hidden="true">
        {checked ? "On" : "Off"}
      </span>
    </button>
  );
}

export interface ChoiceOption<T extends string = string> {
  value: T;
  label: string;
  description?: string;
}

export interface ChoiceProps<T extends string = string> {
  label: string;
  options: readonly ChoiceOption<T>[];
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
  /** The id of text that describes the whole group (`aria-describedby` on the fieldset). */
  describedBy?: string;
}

/** A radio group presented as a fieldset of real `type="radio"` inputs, one per option, sharing
 * one generated `name` — so arrow keys move between them the way they do for any native radio
 * group, with no keyboard handling of our own to get wrong. `disabled` reaches both the fieldset
 * and every radio, so a row that only wraps this in its own `disabled`-gated logic (defence in
 * depth) still sees the control itself grey out. */
export function Choice<T extends string = string>({
  label,
  options,
  value,
  onChange,
  disabled,
  describedBy,
}: ChoiceProps<T>) {
  const uid = useId();
  const name = `${uid}-choice`;
  return (
    <fieldset className="dloop-choice" disabled={disabled} aria-describedby={describedBy}>
      <legend className="dloop-choice-legend">{label}</legend>
      {options.map((option) => {
        const descId = option.description ? `${uid}-${option.value}-desc` : undefined;
        return (
          <label key={option.value} className="dloop-choice-option">
            <input
              type="radio"
              name={name}
              checked={value === option.value}
              aria-labelledby={`${uid}-${option.value}-label`}
              aria-describedby={descId}
              disabled={disabled}
              onChange={() => onChange(option.value)}
            />
            <span className="dloop-choice-text">
              <span className="dloop-choice-label" id={`${uid}-${option.value}-label`}>
                {option.label}
              </span>
              {option.description ? (
                <span className="dloop-choice-desc" id={descId}>
                  {option.description}
                </span>
              ) : null}
            </span>
          </label>
        );
      })}
    </fieldset>
  );
}

/** The open/closed state a Decision-page row's own disclosure uses: controlled by the page's
 * `open`/`onToggle` pair when both are given (the page owns one open record keyed by row
 * id, so it can default most rows closed and one open, and jump to a row by id), or kept in the
 * row's own state otherwise. A row mounted alone, passing neither prop, keeps its own default
 * (the setup rows open, the reach and doing rows closed) — which this falls back to.
 * `initialOpen` only seeds the uncontrolled
 * branch; once a row is controlled, its own `useState` call still runs (Rules of Hooks) but its
 * value is simply unused. */
export function useRowOpen(
  initialOpen: boolean,
  open?: boolean,
  onToggle?: () => void,
): [boolean, () => void] {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(initialOpen);
  if (open !== undefined) return [open, () => onToggle?.()];
  return [uncontrolledOpen, () => setUncontrolledOpen((was) => !was)];
}
