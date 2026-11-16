/**
 * The dictation controls the composer draws under `DictationContext`: the mic button that
 * stands in the pill where the plain glyph is, and the strip above the pill with the elapsed time,
 * the provisional text, Cancel, the GPU fallback line and the notice. Both only draw a
 * `DictationView` and call `DictationControls`; the state lives in `useDictation`.
 *
 * Provisional text and notices are text nodes: they come from a speech engine and the server.
 */

import type { KeyboardEvent, MouseEvent, PointerEvent, ReactElement } from "react";
import { Spinner } from "../components/Loading/Loading";
import type { DictationControls, DictationView } from "./dictationTypes";
import "./Dictation.css";

const ACTIVE = new Set(["starting", "recording", "finalizing"]);

function clock(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export function MicButton({
  view,
  controls,
}: {
  view: DictationView;
  controls: DictationControls;
}): ReactElement {
  const { phase } = view;
  const ready = phase !== "unavailable";
  return (
    <button
      type="button"
      className={`voice-mic ${ready ? "voice-mic-ready" : "voice-mic-unready"}`}
      aria-label="Dictation"
      title="Hold to talk, or click to start and stop. Dictation runs on this computer."
      aria-pressed={phase === "starting" || phase === "recording"}
      aria-disabled={phase === "finalizing" || phase === "cancelling" || undefined}
      // Keeps the focus (and the caret) in the box: the final lands where the caret was.
      onPointerDown={(event: PointerEvent) => {
        event.preventDefault();
        controls.press();
        // Capture sends the pointerup here wherever the pointer ends, so a hold released off the
        // button still stops the recording. jsdom has no setPointerCapture.
        const target = event.currentTarget;
        if (typeof target.setPointerCapture === "function")
          target.setPointerCapture(event.pointerId);
      }}
      onPointerUp={() => controls.release()}
      onPointerCancel={() => controls.release()}
      // A pointer click arrives after pointerup with detail >= 1 and is already handled; detail 0
      // is a keyboard or assistive-technology activation.
      onClick={(event: MouseEvent) => {
        if (event.detail === 0) controls.toggle();
      }}
      onKeyDown={(event: KeyboardEvent) => {
        if (event.key === "Escape") controls.cancel();
      }}
    >
      {phase === "starting" || phase === "recording" ? (
        <span className="voice-mic-stop" aria-hidden="true" />
      ) : phase === "finalizing" ? (
        <Spinner className="voice-mic-spin" />
      ) : (
        <svg
          className="ico"
          width="18"
          height="18"
          viewBox="0 0 16 16"
          aria-hidden="true"
          focusable="false"
        >
          <rect x="5.5" y="1.5" width="5" height="8" rx="2.5" />
          <path d="M3 7.5a5 5 0 0 0 10 0M8 12.5v2" />
        </svg>
      )}
      {view.fallback !== null && (
        <span className="voice-mic-badge" aria-hidden="true">
          CPU
        </span>
      )}
    </button>
  );
}

export function DictationStrip({
  view,
  controls,
}: {
  view: DictationView;
  controls: DictationControls;
}): ReactElement | null {
  const active = ACTIVE.has(view.phase);
  if (!active && view.notice === null) return null;
  return (
    <div className="voice-strip">
      {active && <span role="timer">{clock(view.elapsedMs)}</span>}
      <span className="voice-provisional" aria-live="polite">
        {view.provisional}
      </span>
      {active && (
        <button type="button" className="btn txt sm" onClick={() => controls.cancel()}>
          Cancel
        </button>
      )}
      {view.fallback !== null && (
        <span>
          On the CPU: the GPU failed ({view.fallback.code}){" "}
          <button type="button" className="btn txt sm" onClick={() => controls.retryGpu()}>
            Try GPU again
          </button>
        </span>
      )}
      {view.notice !== null && (
        <span className="voice-notice" role="status">
          {view.notice}{" "}
          <button type="button" className="btn txt sm" onClick={() => controls.dismiss()}>
            Dismiss
          </button>
        </span>
      )}
    </div>
  );
}
