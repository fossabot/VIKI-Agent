import { type ReactElement, useEffect, useRef, useState } from "react";
import { Markdown } from "../Conversation/Markdown";
import { useModalFocus } from "../Dialog/useModalFocus";
import "./Help.css";

export interface TourStep {
  title: string;
  body: string;
  region?: string;
}

export interface TourProps {
  steps: TourStep[];
  onClose: () => void;
}

// Tour copy is onboarding text, never a chat reply, and Tour owns no app action to open a link
// with (Markdown's onOpenUrl is required, never optional). A link would still render as safe,
// visible text — Markdown's own URL-scheme gate runs regardless — it would just not navigate.
function noOpenUrl(): void {}

/** The onboarding walkthrough: a small card docked over the corner of the app, not a
 * full-screen dialog, so the real UI a step points at stays visible underneath. `useModalFocus`
 * still traps Tab and Escape and inerts the background (no visual dim, but the rest of the app
 * is not reachable while a step is showing) the same as every other dialog in this app. */
export function Tour({ steps, onClose }: TourProps): ReactElement | null {
  const [index, setIndex] = useState(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const step = steps[index];

  useModalFocus({
    active: true,
    dialogRef,
    initialFocusRef: nextRef,
    onDismiss: onClose,
  });

  // Keyed on the region string itself, not the step or the steps array, so a parent that
  // recreates `steps` every render (a literal in its own JSX) does not retrigger this for a step
  // that has not actually changed. Cleanup (step change, or unmount) always removes the class it
  // added before a later step's effect, if any, looks for its own target.
  const region = step?.region;
  useEffect(() => {
    if (!region) return;
    const target = document.querySelector(`[data-region="${CSS.escape(region)}"]`);
    if (!target) return;
    target.classList.add("tour-highlight");
    return () => target.classList.remove("tour-highlight");
  }, [region]);

  if (!step) return null;

  const isFirst = index === 0;
  const isLast = index === steps.length - 1;

  function handleBack(): void {
    setIndex((current) => current - 1);
  }

  function handleNext(): void {
    if (isLast) {
      onClose();
      return;
    }
    setIndex((current) => current + 1);
  }

  return (
    <div
      ref={dialogRef}
      className="tour"
      role="dialog"
      aria-modal="true"
      aria-labelledby="tour-title"
      tabIndex={-1}
    >
      <p className="help-status jp">{`Step ${index + 1} of ${steps.length}`}</p>
      <h2 id="tour-title" className="jp">
        {step.title}
      </h2>
      <Markdown text={step.body} onOpenUrl={noOpenUrl} />
      <div className="help-actions">
        <button type="button" className="btn txt sm tour-skip" onClick={onClose}>
          Skip tour
        </button>
        {!isFirst ? (
          <button type="button" className="btn alt sm" onClick={handleBack}>
            Back
          </button>
        ) : null}
        <button type="button" className="btn go sm" ref={nextRef} onClick={handleNext}>
          {isLast ? "Done" : "Next"}
        </button>
      </div>
    </div>
  );
}
