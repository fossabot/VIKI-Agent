import type { ReactNode } from "react";

export interface RailSectionProps {
  title: string;
  /** Shown top-right, e.g. an open count. Omitted when absent. */
  count?: ReactNode;
  /** Dim the count instead of orange — for a count that isn't an action needing attention
   * (e.g. Routines' next-run label). */
  dim?: boolean;
  /** Extra class on the section itself, for one-off spacing (e.g. Routines' 16px gap). */
  className?: string;
  children?: ReactNode;
}

/** A labelled section of the right rail: an uppercase `lbl` title, an optional count, and
 * whatever content the section holds. */
export function RailSection({ title, count, dim = false, className, children }: RailSectionProps) {
  return (
    <section className={className ? `rail-section ${className}` : "rail-section"}>
      <div className="rail-section-head">
        <span className="lbl">{title}</span>
        {count !== undefined ? (
          <span
            className={dim ? "rail-section-count rail-section-count--dim" : "rail-section-count"}
          >
            {count}
          </span>
        ) : null}
      </div>
      <div className="rail-section-body">{children}</div>
    </section>
  );
}
