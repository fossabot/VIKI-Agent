/**
 * The loading kit: one
 * spinner, one check, one delayed text line and one busy-button spinner, so every wait in the
 * app reads the same way. Each export names its one-place switch:
 *   - useHeld: the show/hold rule for any indicator that is mounted or unmounted in place (a
 *     skeleton, a busy button's spinner) rather than merely faded by CSS. Switch:
 *     SHOW_DELAY_MS, MIN_VISIBLE_MS below.
 *   - Spinner: a growing arc, the one spinner for the whole app. Switch: --spinner-lap /
 *     --spinner-lap-reduced (styles/tokens.css), read by Loading.css.
 *   - Check: the small confirmation tick shown after a save. No switch; a fixed path.
 *   - Loading: a plain text line, shown and hidden purely by Loading.css's own delay (no JS
 *     hold: nothing here is replaced in place). Switch: --loading-delay (styles/tokens.css).
 *   - BusyLabel: grows a held spinner in front of a button's label without changing the
 *     label itself. Switch: useHeld, same as above.
 */
import { type ReactNode, useEffect, useState } from "react";
import "./Loading.css";

/** How long a wait stays invisible before Loading.css starts fading its indicator in. Mirrored
 * by styles/tokens.css's --loading-delay (the two are pinned equal in
 * Loading.css.unit.test.ts). */
export const SHOW_DELAY_MS = 300;
/** Once shown, the least time an indicator that useHeld gates stays mounted. */
export const MIN_VISIBLE_MS = 400;
/** The latest moment, measured from when a wait began, that useHeld can still be holding. */
const RELEASE_MS = SHOW_DELAY_MS + MIN_VISIBLE_MS;

interface HeldState {
  /** When the wait that is currently governing `held` began. */
  startedAt: number;
  held: boolean;
}

/** Pure: the next `{ startedAt, held }` given the live `pending`, the previously computed state
 * (null on the first render) and the current time. See `useHeld`'s own comment for the rule.
 *
 * While idle (not pending, not held) this returns `prev` itself, unchanged, rather than a fresh
 * object: `startedAt` only matters from the moment a wait begins, which the `pending` branch
 * below already stamps, so there is nothing for an idle render to recompute. Returning a new
 * `{ startedAt: now, ... }` here instead would make every idle render see its own
 * `now` as a "change" from the previous state and call `setState` during render to chase it —
 * one `useHeld` call does not already converge within a single commit's render-phase-update
 * retries, since `now` never repeats, and React throws "Too many re-renders" once a component
 * is slow enough that the real clock moves between retries (fake timers hide this: Date.now()
 * stands still between them, so the chase stops after exactly one retry in tests). */
function nextHeld(pending: boolean, prev: HeldState | null, now: number): HeldState {
  if (pending) {
    // A wait starting fresh gets `now`; one starting mid-hold keeps the first start.
    return { startedAt: prev?.held ? prev.startedAt : now, held: true };
  }
  if (!prev) return { startedAt: now, held: false }; // the very first render only
  if (!prev.held) return prev; // idle: nothing to recompute
  const elapsed = now - prev.startedAt;
  const stillHolding = elapsed >= SHOW_DELAY_MS && elapsed < RELEASE_MS;
  return { startedAt: prev.startedAt, held: stillHolding };
}

/**
 * True while `pending`. Once `pending` drops, a wait that had already reached `SHOW_DELAY_MS`
 * (so an indicator gated on this value actually became visible) stays true until
 * `SHOW_DELAY_MS + MIN_VISIBLE_MS` after it began, so a resolve right after showing doesn't
 * flash. A wait that never reached `SHOW_DELAY_MS`, or has already outlasted that window, drops
 * at once. A new `pending` spell starting mid-hold does not reset the clock — the hold still
 * counts from the original start.
 *
 * The returned value is computed fresh every render from `pending`, the previous computed state
 * and the current time, and pushed into state during render when it changes (React's "adjust
 * state while rendering" pattern) rather than from an effect: an effect-driven version would
 * return the previous, stale value for one extra frame right after `pending` flips, so anything
 * keyed on it (whether a held indicator is in the tree at all) would unmount and remount a frame
 * later — restarting any CSS delay the indicator itself carries. Only the *wake-up* at the
 * hold's deadline needs an effect, since nothing else re-renders this component exactly when
 * that deadline passes.
 */
export function useHeld(pending: boolean): boolean {
  const [state, setState] = useState<HeldState>(() => nextHeld(pending, null, Date.now()));

  const computed = nextHeld(pending, state, Date.now());
  if (computed.held !== state.held || computed.startedAt !== state.startedAt) {
    setState(computed);
  }

  useEffect(() => {
    // `pending` must be a dep even though a pending spell ending while already held (true ->
    // true) changes neither `computed.held` nor `computed.startedAt`: without it, React would
    // see no dependency change on that transition and never schedule the wake-up below. Because
    // it is a dep, this effect (and its closure over `pending`) is always rebuilt the instant
    // `pending` changes again, before the old timeout could fire — so the plain closure here
    // never goes stale and needs no ref.
    if (!computed.held || pending) return undefined;
    const delay = Math.max(computed.startedAt + RELEASE_MS - Date.now(), 0);
    const id = setTimeout(() => setState((prev) => nextHeld(pending, prev, Date.now())), delay);
    return () => clearTimeout(id);
  }, [pending, computed.held, computed.startedAt]);

  return computed.held;
}

/** A growing arc, 14 px, drawn in `currentColor` so each site colours it with its own text
 * colour. `className` adds the site's own class alongside the base "spinner" class;
 * Loading.css's reduced-motion rules key off "spinner" alone, so every site keeps the
 * slow-not-stopped behaviour regardless of its extra class. */
export function Spinner({ className }: { className?: string }) {
  return (
    <span className={className ? `spinner ${className}` : "spinner"} aria-hidden="true">
      <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
        <circle className="spinner-track" cx="8" cy="8" r="6" />
        <circle className="spinner-arc" cx="8" cy="8" r="6" />
      </svg>
    </span>
  );
}

/** The confirmation tick shown after a save, 14 px, `currentColor`. */
export function Check({ className }: { className?: string }) {
  return (
    <svg
      className={className ? `check-mark ${className}` : "check-mark"}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3.5 8.5l3 3 6-7" />
    </svg>
  );
}

/**
 * A plain status line: a spinner beside words, delayed-revealed purely by Loading.css (no
 * `useHeld` — there is nothing here to mount or unmount in place, so a CSS show-delay is
 * enough). `children` renders as a direct text child beside the `aria-hidden` spinner, so
 * `getByText` and the accessible name stay exactly the given words. `inline` renders a `span`
 * instead of a `p`, for use inside running text; `announce=false` drops the `status` role for
 * sites that already announce some other way.
 */
export function Loading({
  children,
  className,
  id,
  inline,
  announce = true,
}: {
  children: ReactNode;
  className?: string;
  id?: string;
  inline?: boolean;
  announce?: boolean;
}) {
  const Tag = inline ? "span" : "p";
  return (
    <Tag
      className={className ? `loading ${className}` : "loading"}
      id={id}
      role={announce ? "status" : undefined}
    >
      <Spinner />
      {children}
    </Tag>
  );
}

/**
 * A button's label, with a held spinner grown in front of it while `busy` — the label text
 * itself never changes, so the button's accessible name stays whatever `children` already was.
 * `useHeld` (not `busy` directly) gates the spinner so a busy spell that resolves just after
 * showing one doesn't flash it. `failed` drops a held spinner at once when the wait ended in a
 * failure the page is showing, since the hold is only for a success landing just after the
 * spinner appeared.
 */
export function BusyLabel({
  busy,
  failed = false,
  children,
}: {
  busy: boolean;
  failed?: boolean;
  children: ReactNode;
}) {
  const held = useHeld(busy);
  return (
    <>
      {held && !failed ? <Spinner className="spinner-late busy-spinner" /> : null}
      {children}
    </>
  );
}
