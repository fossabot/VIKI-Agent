import "./ContextSummary.css";

export interface ContextSummaryProps {
  percent?: number;
  onOpen?: () => void;
}

/** Uses only the selected conversation's observed context percentage. */
export function ContextSummary({ percent, onOpen }: ContextSummaryProps) {
  const usage = percent === undefined ? "Not reported" : `${percent}% used`;
  const value = percent === undefined ? undefined : Math.min(100, Math.max(0, percent));
  const content = (
    <>
      <span className="context-summary-heading">
        <span>Context window</span>
        <span className="context-summary-value">{usage}</span>
      </span>
      {value !== undefined ? (
        // biome-ignore lint/a11y/useSemanticElements: Native meter painting collapses the 5px fill.
        <span
          className="context-summary-track"
          role="meter"
          aria-label="Context window used"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={value}
        >
          <span
            className="context-summary-fill"
            aria-hidden="true"
            style={{ width: `${value}%` }}
          />
        </span>
      ) : null}
      <span className="context-summary-detail">
        {onOpen ? "Context & loadout" : "Context & loadout unavailable"}
        {onOpen ? <span aria-hidden="true"> ↗</span> : null}
      </span>
    </>
  );
  return onOpen ? (
    <button
      type="button"
      className="context-summary"
      aria-label={`Context & loadout: ${usage}`}
      onClick={onOpen}
    >
      {content}
    </button>
  ) : (
    <div className="context-summary">{content}</div>
  );
}
