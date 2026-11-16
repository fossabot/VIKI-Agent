/**
 * The launcher: the floating circle that shows and hides the panel. It owns no data of its
 * own — `view` is the server's `LauncherView`, kept fresh
 * by its caller; `position` is whatever `useLauncherPosition` returned for this
 * mount, spread straight onto the button; `typingUntilMs`/`reducedMotion` are presence and motion
 * facts the caller already tracks (the latter folding together the OS preference and the app's
 * own Settings › Appearance override, the same two signals `ActivityOrb/useActivityPresentation.ts`
 * reads). This file only turns those into one of eight "looks" plus an independent "is VIKI
 * working" overlay, and an accessible name, and keeps the two per-process counters (`nudge`,
 * `glow`) from replaying their cue on every render.
 *
 * Look (`resolveLook`): `off`/`paused` always win — dimmed, but still a button that opens the
 * panel (an off launcher still opens; `open` plays no part in that, a click always toggles). Else
 * the server's own `level` wins, with one exception: at `level: "nudge"`, the look is the
 * *badge* (the item count, no ring) until this component is ready to play that nudge's pulse,
 * and `nudge` only for the 700 ms (2 s under reduced motion) the pulse is actually on screen.
 * A nudge is "held while typing ... then the badge" — there is no separate resting "nudge
 * ring" to draw in between, so the badge rendering already covers it: nudge items are already
 * counted in `view.count`. `open` does not change which look is shown: the next `view` the
 * caller fetches after opening is what actually clears a loud level, not a local rule
 * here.
 *
 * Working overlay (`showsWorkingArc`): a running chat matters most exactly when there is also an
 * item open about it, so the turning arc is not its own look — it layers on top of idle, list,
 * badge or question (the count or "?" badge keeps rendering underneath it) whenever
 * `view.working > 0`. It is hidden while a nudge pulse is actually playing or the alert ring is
 * up (both already read as their own, louder look), and in off/paused. The arc is a *separate*
 * element from the look's own ring (`.dloop-launcher-arc`, not `.dloop-launcher-ring`) so its
 * spin can never fight list's brighten or nudge's pulse for the same `border-color`/`box-shadow`
 * animation on one element — e.g. list (brightening) and working can both be true at once, each
 * animating its own element. The accessible name joins the look's own name with the working
 * clause, e.g. "VIKI, 2 items need you, working on 1 chat".
 *
 * Nudge and glow cues (the launcher plays its cue once per increase): each is detected
 * by comparing the incoming `view.nudge`/`view.glow` against a ref holding the previous one, so
 * the counter's value on the very first render is the baseline, never a cue — a page load can
 * arrive mid-count, and that first number is not "a new one arriving". A nudge increase does not
 * play immediately if `Date.now() < typingUntilMs` (the user is typing); the effect re-reads
 * `Date.now()` against the current `typingUntilMs` whenever either changes, so each keystroke
 * that pushes `typingUntilMs` further out reschedules the same wait rather than firing early —
 * there is no separate bound on how long this can wait, because the thing being waited for
 * (typing) is exactly what keeps extending it. A glow increase has no such hold; it always plays.
 *
 * Reduced motion turns the nudge/glow cues into the same plain fade (`dloop-launcher-cue-fade`, a
 * CSS `transition`, never a `@keyframes` animation) instead of their own animation
 * (`dloop-launcher-cue-pulse`/`-brighten`). The working arc instead simply stops spinning (no
 * `dloop-launcher-cue-spin` class) and shows as "a still partial ring" — it has no single moment
 * to fade at, since it is not a one-shot counter-driven cue. There is no sound either way;
 * nothing here plays any.
 */
import { type ReactElement, useEffect, useRef, useState } from "react";
import type { LauncherView } from "../../clients/decisionLoop/types";
import type { UseLauncherPositionResult } from "./useLauncherPosition";
import "./Launcher.css";

/** Launcher.css's own width and height: what `useLauncherPosition` keeps inside the window,
 * and what the panel opens beside. */
export const LAUNCHER_SIZE = 100;

/** How long a played nudge pulse stays on screen; matches Launcher.css's `dloop-launcher-pulse`
 * keyframe duration. */
const PULSE_MS = 700;
/** How long a played glow brighten stays on screen; matches Launcher.css's
 * `dloop-launcher-brighten` keyframe duration. */
const BRIGHTEN_MS = 6000;
/** Reduced motion's one shared cue length, in place of either duration above. */
const REDUCED_CUE_MS = 2000;

export interface LauncherProps {
  view: LauncherView;
  open: boolean;
  onToggle: () => void;
  position: UseLauncherPositionResult;
  /** `Date.now()` ms until which the user is treated as typing; see the module doc. */
  typingUntilMs: number;
  reducedMotion: boolean;
  /** The keyboard shortcut that opens or closes VIKI's panel (`useLauncherShortcuts`): shown
   * as `aria-keyshortcuts` and the button's tooltip. Absent, neither attribute is set. */
  shortcut?: { aria: string; label: string };
}

type LauncherLook = "idle" | "list" | "badge" | "question" | "nudge" | "alert" | "off" | "paused";

/** The looks the working arc may sit on top of; see the module doc's "Working overlay" section. */
const WORKING_ARC_LOOKS: ReadonlySet<LauncherLook> = new Set(["idle", "list", "badge", "question"]);

/** Which of the eight looks this render shows; see the module doc for the `nudge` interpretation.
 * `pulsing` is whether a held or fresh nudge pulse is on screen right now. */
function resolveLook(view: LauncherView, pulsing: boolean): LauncherLook {
  if (view.state === "off") return "off";
  if (view.state === "paused") return "paused";
  if (view.level === "nudge") return pulsing ? "nudge" : "badge";
  return view.level;
}

/** Whether the working arc overlays this render's look; see the module doc. */
function showsWorkingArc(look: LauncherLook, working: number): boolean {
  return working > 0 && WORKING_ARC_LOOKS.has(look);
}

function itemsNeedYou(count: number): string {
  return count === 1 ? "1 item needs you" : `${count} items need you`;
}

function workingOnChats(count: number): string {
  return count === 1 ? "working on 1 chat" : `working on ${count} chats`;
}

/** The look's own name, before any working clause is joined onto it. */
function baseAccessibleName(view: LauncherView, look: LauncherLook): string {
  switch (look) {
    case "off":
      return "VIKI, proactive VIKI is off";
    case "paused":
      return "VIKI, paused";
    case "question":
      return "VIKI, a question for you";
    case "badge":
    case "nudge":
    case "alert":
      return `VIKI, ${itemsNeedYou(view.count)}`;
    case "list":
      return "VIKI, new information available";
    case "idle":
      return "VIKI, nothing needs you right now";
  }
}

/** "VIKI" plus the state in words: the button's whole accessible name — it has no
 * visible text besides the badge digit or "?", so nothing else contributes to it. Nudge and alert
 * read exactly like badge, "N items need you": that is the fact itself, and only the ring's
 * motion (or lack of it) tells the three urgencies apart, which a sighted user already sees.
 * When the working arc is also showing, its clause joins on, e.g. "VIKI, 2 items need you,
 * working on 1 chat". */
function accessibleName(view: LauncherView, look: LauncherLook, workingArc: boolean): string {
  const base = baseAccessibleName(view, look);
  return workingArc ? `${base}, ${workingOnChats(view.working)}` : base;
}

/** The transient class that plays this render's nudge/glow cue, if any — never the working arc,
 * which is driven separately (see the component body) since it is not a one-shot counter-driven
 * cue. Reduced motion substitutes the one shared fade for whichever real animation would have
 * played, scoped to the same look match, so a brighten that outlives its `list` look (the level
 * having moved on) never bleeds a fade onto whatever look is current. */
function transientCueClass(
  look: LauncherLook,
  pulsing: boolean,
  brightening: boolean,
  reducedMotion: boolean,
): string {
  const nudgeCue = look === "nudge" && pulsing;
  const listCue = look === "list" && brightening;
  if (reducedMotion) return nudgeCue || listCue ? "dloop-launcher-cue-fade" : "";
  if (nudgeCue) return "dloop-launcher-cue-pulse";
  if (listCue) return "dloop-launcher-cue-brighten";
  return "";
}

export function Launcher({
  view,
  open,
  onToggle,
  position,
  typingUntilMs,
  reducedMotion,
  shortcut,
}: LauncherProps): ReactElement {
  const [pulsing, setPulsing] = useState(false);
  const [brightening, setBrightening] = useState(false);
  // Lazy initial values: each captures *this* mount's own view as its baseline, the same render
  // whose number must never itself count as "a new one arriving" (the module doc's mount rule).
  const prevNudgeRef = useRef(view.nudge);
  const prevGlowRef = useRef(view.glow);
  const pendingPulseRef = useRef(false);
  const pulseEndTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const brightenEndTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Final teardown only: a reschedule mid-life clears and replaces these two timers itself (see
  // below); only unmount needs this, since nothing else ever returns a cleanup for them.
  useEffect(() => {
    return () => {
      if (pulseEndTimerRef.current !== null) clearTimeout(pulseEndTimerRef.current);
      if (brightenEndTimerRef.current !== null) clearTimeout(brightenEndTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (view.nudge !== prevNudgeRef.current) {
      prevNudgeRef.current = view.nudge;
      pendingPulseRef.current = true;
    }
    if (!pendingPulseRef.current) return;

    function firePulse(): void {
      pendingPulseRef.current = false;
      setPulsing(true);
      if (pulseEndTimerRef.current !== null) clearTimeout(pulseEndTimerRef.current);
      pulseEndTimerRef.current = setTimeout(
        () => {
          pulseEndTimerRef.current = null;
          setPulsing(false);
        },
        reducedMotion ? REDUCED_CUE_MS : PULSE_MS,
      );
    }

    const wait = typingUntilMs - Date.now();
    if (wait <= 0) {
      firePulse();
      return;
    }
    // Still typing: wait exactly until `typingUntilMs`. A later keystroke pushes it out again,
    // which reruns this effect (its dependency changed) and reschedules from the new value.
    const timer = setTimeout(firePulse, wait);
    return () => clearTimeout(timer);
  }, [view.nudge, typingUntilMs, reducedMotion]);

  useEffect(() => {
    if (view.glow === prevGlowRef.current) return;
    prevGlowRef.current = view.glow;
    setBrightening(true);
    if (brightenEndTimerRef.current !== null) clearTimeout(brightenEndTimerRef.current);
    brightenEndTimerRef.current = setTimeout(
      () => {
        brightenEndTimerRef.current = null;
        setBrightening(false);
      },
      reducedMotion ? REDUCED_CUE_MS : BRIGHTEN_MS,
    );
  }, [view.glow, reducedMotion]);

  const look = resolveLook(view, pulsing);
  const workingArc = showsWorkingArc(look, view.working);
  const className = [
    "dloop-launcher",
    `dloop-launcher-look-${look}`,
    workingArc ? "dloop-launcher-working" : "",
    workingArc && !reducedMotion ? "dloop-launcher-cue-spin" : "",
    transientCueClass(look, pulsing, brightening, reducedMotion),
  ]
    .filter(Boolean)
    .join(" ");

  // `endedDrag()` reads and clears a one-shot flag (useLauncherPosition's own doc): calling it
  // here is exactly its intended use, so the click a browser fires right after a drag's
  // pointer-up never also toggles the panel.
  function handleClick(): void {
    if (position.endedDrag()) return;
    onToggle();
  }

  return (
    <button
      type="button"
      className={className}
      style={{ right: position.right, bottom: position.bottom }}
      data-dragging={position.dragging}
      aria-label={accessibleName(view, look, workingArc)}
      aria-expanded={open}
      aria-haspopup="dialog"
      aria-keyshortcuts={shortcut?.aria}
      title={shortcut ? `VIKI's panel (${shortcut.label})` : undefined}
      onClick={handleClick}
      {...position.bind}
    >
      <img
        className="dloop-launcher-mark"
        src="/brand/mark.png?v=8085d423"
        alt=""
        draggable={false}
      />
      <span className="dloop-launcher-ring" aria-hidden="true" />
      {workingArc ? <span className="dloop-launcher-arc" aria-hidden="true" /> : null}
      {look === "badge" ? <span className="dloop-launcher-badge">{view.count}</span> : null}
      {look === "question" ? <span className="dloop-launcher-badge">?</span> : null}
    </button>
  );
}
