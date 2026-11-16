/**
 * The Decision page's Proactive VIKI card: the one place that turns proactive VIKI on or off and
 * sets Pause and Focus. Not a collapsible row, so it has no disclosure; the page shows the one
 * read-only notice, so the card has none.
 *
 * Off, the card says so and offers only "Turn proactive VIKI on". On, it shows the switch, the
 * launcher's status line (`statusLine`, `../wording`), the Pause or Resume buttons, one Focus
 * button and a hint saying what Pause and Focus do. Every write is `patch` then `onRefresh`, and
 * a control that a write replaces hands focus to its replacement once the next `page` renders it.
 */
import { type RefObject, useEffect, useRef, useState } from "react";
import type { PageView } from "../../../clients/decisionLoop/types";
import { t } from "../../../i18n/t";
import { Switch } from "../DecisionRow";
import { statusLine } from "../wording";
import "./ProactiveCard.css";

export interface ProactiveCardProps {
  page: PageView;
  writable: boolean;
  patch: (changes: Record<string, unknown>) => Promise<unknown>;
  onRefresh: () => void;
  /** Milliseconds since the epoch, the base of Pause and Focus stamps and of the status line's
   * "checked ... ago"; defaults to `Date.now()` so a test can pin it. */
  nowMs?: number;
}

const HOUR_MS = 60 * 60 * 1000;

/** The controls a write can replace, and so the ones focus can move to. */
type Replacement = "switch" | "turnOn" | "pause" | "resume";

/** 08:00 local time on the day after `nowMs`, as a UTC stamp. */
function tomorrowMorning(nowMs: number): string {
  const now = new Date(nowMs);
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 8, 0, 0, 0).toISOString();
}

export function ProactiveCard({
  page,
  writable,
  patch,
  onRefresh,
  nowMs = Date.now(),
}: ProactiveCardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const switchRef = useRef<HTMLButtonElement>(null);
  const turnOnRef = useRef<HTMLButtonElement>(null);
  const pauseRef = useRef<HTMLButtonElement>(null);
  const resumeRef = useRef<HTMLButtonElement>(null);
  const replacements: Record<Replacement, RefObject<HTMLButtonElement | null>> = {
    switch: switchRef,
    turnOn: turnOnRef,
    pause: pauseRef,
    resume: resumeRef,
  };
  // The control the last write replaced; focused as soon as the page renders it, then cleared.
  const pendingFocus = useRef<Replacement | null>(null);

  useEffect(() => {
    if (pendingFocus.current === null) return;
    const target = replacements[pendingFocus.current].current;
    // A disabled button ignores focus(); the render that ends the write tries again.
    if (target === null || target.disabled) return;
    target.focus();
    pendingFocus.current = null;
  });

  async function run(changes: Record<string, unknown>, replacement?: Replacement): Promise<void> {
    if (!writable) return;
    setBusy(true);
    setError(null);
    try {
      await patch(changes);
      if (replacement !== undefined) pendingFocus.current = replacement;
      onRefresh();
    } catch {
      pendingFocus.current = null;
      setError("Could not save. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const paused = page.paused_until !== "";
  const inFocus = page.focus_until !== "";
  const disabled = !writable || busy;

  return (
    <section className="dloop-card" aria-labelledby="proactive-heading">
      <div className="dloop-card-head">
        <h2 id="proactive-heading" className="dloop-card-title">
          Proactive VIKI
        </h2>
        {page.enabled ? (
          <Switch
            ref={switchRef}
            checked
            labelledBy="proactive-heading"
            disabled={disabled}
            onChange={() => void run({ "decision.enabled": false }, "turnOn")}
          />
        ) : null}
      </div>
      {page.enabled ? (
        <>
          <p className="dloop-card-status">
            {statusLine(
              {
                state: paused ? "paused" : "on",
                paused_until: page.paused_until || null,
                focus_until: page.focus_until || null,
                checked_at: page.status.checked_at,
                open: page.status.open,
                plans_waiting: 0,
                model: page.model.state,
              },
              nowMs,
            )}
          </p>
          <div className="dloop-button-row">
            {paused ? (
              <button
                ref={resumeRef}
                type="button"
                className="btn alt"
                disabled={disabled}
                onClick={() => void run({ "decision.paused_until": "" }, "pause")}
              >
                Resume
              </button>
            ) : (
              <>
                <button
                  ref={pauseRef}
                  type="button"
                  className="btn alt"
                  disabled={disabled}
                  onClick={() =>
                    void run(
                      { "decision.paused_until": new Date(nowMs + HOUR_MS).toISOString() },
                      "resume",
                    )
                  }
                >
                  Pause for 1 hour
                </button>
                <button
                  type="button"
                  className="btn alt"
                  disabled={disabled}
                  onClick={() =>
                    void run({ "decision.paused_until": tomorrowMorning(nowMs) }, "resume")
                  }
                >
                  Pause until tomorrow
                </button>
              </>
            )}
            <button
              type="button"
              className="btn alt"
              disabled={disabled}
              onClick={() =>
                void run({
                  "decision.focus_until": inFocus ? "" : new Date(nowMs + HOUR_MS).toISOString(),
                })
              }
            >
              {inFocus ? "End focus" : "Focus for 1 hour"}
            </button>
          </div>
          <p className="dloop-hint">{t("decision_page.proactive.hint")}</p>
        </>
      ) : (
        <>
          <p>{t("decision_page.proactive.off")}</p>
          <div className="dloop-button-row">
            <button
              ref={turnOnRef}
              type="button"
              className="btn go"
              disabled={disabled}
              onClick={() => void run({ "decision.enabled": true }, "switch")}
            >
              Turn proactive VIKI on
            </button>
          </div>
        </>
      )}
      {error ? (
        <p className="dloop-hint" role="status">
          {error}
        </p>
      ) : null}
    </section>
  );
}
