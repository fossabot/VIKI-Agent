import { type ReactNode, useId } from "react";
import "./Field.css";

interface ControlProps {
  id: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

export function Field({
  label,
  hint,
  more,
  error,
  group = false,
  children,
}: {
  label: string;
  hint?: string | undefined;
  more?: { summary: string; text: string } | undefined;
  error?: string | null | undefined;
  group?: boolean;
  children(control: ControlProps): ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const described = [hint ? hintId : "", error ? errorId : ""].filter(Boolean).join(" ");
  const control: ControlProps = { id };
  if (group) control["aria-labelledby"] = `${id}-label`;
  if (described) control["aria-describedby"] = described;
  if (error) control["aria-invalid"] = true;
  return (
    <div className="field">
      {group ? (
        <span className="field-label" id={`${id}-label`}>
          {label}
        </span>
      ) : (
        <label className="field-label" htmlFor={id}>
          {label}
        </label>
      )}
      {hint ? (
        <p className="section-hint" id={hintId}>
          {hint}
        </p>
      ) : null}
      {children(control)}
      {more ? (
        <details className="field-more">
          <summary>{more.summary}</summary>
          <p className="section-hint">{more.text}</p>
        </details>
      ) : null}
      {error ? (
        <p className="field-error" id={errorId} role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
