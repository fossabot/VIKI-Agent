/**
 * The copy affordance, for a reply and for a code block.
 *
 * Contract is frozen in src/accept/m4s/copy.accept.test.tsx; read that first, it is the
 * specification.
 *
 * Shape, so the two callers can share one control:
 * - `getText` is read at CLICK time, not at render time. That is what lets a code block pass
 *   `() => element.textContent` and copy exactly what is on screen, rather than reconstructing
 *   the source from react-markdown's node tree — which would have to track remark's shapes
 *   forever and would still disagree with the rendering the user is looking at.
 * - `label` is the accessible name ("Copy reply"/"Copy code"). The VISIBLE text is
 *   always the short "Copy", because a code block's own control must not shout over the code.
 *   `aria-label` overrides the accessible name outright, so the label stays fixed while the
 *   visible text below moves through its three states — a screen reader announces the control by
 *   what it does, not by which state it happens to be in.
 *
 * Never throws or rejects out of the click handler (the clipboard is a permission-gated API and
 * can refuse at any time): `getText` itself is called inside a try/catch, and the write's
 * rejection is caught by a second `.then` argument rather than left to become an unhandled
 * rejection.
 *
 * Every status change here happens inside a promise callback or a timer callback — never inside
 * React's own click-event dispatch — so React has no reason to treat it as urgent and will only
 * get around to painting it on its own schedule. `flushSync` forces the paint through at the
 * point each change actually happens, which is what lets a reset that fires under advanced fake
 * timers be visible to a synchronous assertion the instant it fires, with no `act()` of the
 * caller's own required.
 *
 * The design does not draw this surface, so its wording and its states follow the design's own
 * grammar.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import "./Copy.css";

export interface CopyButtonProps {
  /** Read at click time. See the header: a code block passes the rendered element's textContent. */
  getText: () => string;
  /** The accessible name. The visible label stays "Copy". */
  label: string;
  /** Defaults to the quiet `copy-btn` look; a caller sitting among ordinary `.btn` controls
   * (the Providers page's setup commands) passes its own classes instead. */
  className?: string;
}

type Status = "idle" | "done" | "failed";

const VISIBLE_TEXT: Readonly<Record<Status, string>> = {
  idle: "Copy",
  done: "Copied",
  failed: "Copy failed",
};

/** How long the confirmation (or refusal) stays up before the control goes quiet again on its
 * own — long enough to read, short enough that it never reads as a permanent second label. */
const SETTLE_MS = 2000;

export function CopyButton({
  getText,
  label,
  className = "copy-btn",
}: CopyButtonProps): ReactElement {
  const [status, setStatus] = useState<Status>("idle");
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Unmounting mid-flight (the turn's reply scrolls out and React tears it down) must not fire a
  // setState on a gone component.
  useEffect(() => () => clearTimeout(timerRef.current), []);

  function settle(next: Status): void {
    clearTimeout(timerRef.current);
    flushSync(() => setStatus(next));
    timerRef.current = setTimeout(() => flushSync(() => setStatus("idle")), SETTLE_MS);
  }

  function handleClick(): void {
    let text: string;
    try {
      text = getText();
    } catch {
      settle("failed");
      return;
    }
    // The METHOD, not the object. `navigator.clipboard` exists in contexts that do not expose
    // writeText, and a test double is free to provide the object with nothing on it — in both
    // cases `!navigator.clipboard` is false and the call below throws synchronously, out of the
    // click handler, which is the one thing the contract says must never happen.
    const write = navigator.clipboard?.writeText;
    if (typeof write !== "function") {
      settle("failed"); // no permission, no secure context, or nothing to call
      return;
    }
    navigator.clipboard.writeText(text).then(
      () => settle("done"),
      () => settle("failed"), // the user denied the permission, or focus moved away mid-click
    );
  }

  return (
    <button type="button" className={className} aria-label={label} onClick={handleClick}>
      {VISIBLE_TEXT[status]}
    </button>
  );
}
