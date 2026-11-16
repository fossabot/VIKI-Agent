import "./ContextBar.css";

/** One category's share of the window, e.g. `{ label: "System prompt", tokens: 4200 }`. */
export interface ContextBarSegment {
  label: string;
  tokens: number;
}

export interface ContextBarProps {
  /** Non-empty categories only — the caller filters out zero-token, deferred and free ones
   * before this component ever sees the list; it does not special-case any label by name. */
  used: readonly ContextBarSegment[];
  max: number;
  /** The buffer held back ahead of the hard limit, or null/omitted to draw none. */
  reserved?: number | null;
  /** The auto-compact threshold, or null/omitted to draw no mark. */
  mark?: number | null;
  size?: "small" | "large";
  /** The bar's own accessible name (role="img"), not any one segment's label. */
  label: string;
}

/** Tone per category; anything not listed reads as --accent-slate,
 * same as "System prompt" — a new category added elsewhere degrades to a neutral tone here
 * rather than going unstyled. */
const TONE_BY_LABEL = new Map([
  ["System prompt", "--accent-slate"],
  ["System tools", "--accent-blue"],
  ["MCP tools", "--accent-purple"],
  ["Memory files", "--or-hi"],
  ["Skills", "--accent-green"],
  ["Messages", "--or"],
]);

export function contextTone(segmentLabel: string): string {
  return TONE_BY_LABEL.get(segmentLabel) ?? "--accent-slate";
}

/** A share of `max`, as a percentage capped at 100 (one category, or the reserved buffer, can
 * never visually overrun the bar even if its raw count does). */
export function contextShare(tokens: number, max: number): number {
  return Math.min(100, (tokens / max) * 100);
}

/** The segmented context-usage meter: a single `role="img"` bar standing in for the whole
 * picture, so a screen reader hears `label` once rather than one announcement per segment. */
export function ContextBar({
  used,
  max,
  reserved = null,
  mark = null,
  size = "large",
  label,
}: ContextBarProps) {
  const showSegments = max > 0;
  return (
    <div className={`context-bar context-bar-${size}`} role="img" aria-label={label}>
      {showSegments &&
        used
          .filter((segment) => segment.tokens > 0)
          .map((segment) => (
            <span
              key={segment.label}
              className="context-bar-segment"
              style={{
                width: `${contextShare(segment.tokens, max)}%`,
                background: `var(${contextTone(segment.label)})`,
              }}
            />
          ))}
      {showSegments && reserved !== null && reserved > 0 && (
        <span
          className="context-bar-reserved"
          style={{ width: `${contextShare(reserved, max)}%` }}
        />
      )}
      {showSegments && mark !== null && (
        <span
          className="context-bar-mark"
          aria-hidden="true"
          style={{ left: `${contextShare(mark, max)}%` }}
        />
      )}
    </div>
  );
}
