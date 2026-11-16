/**
 * Which rails are docked at the current viewport width.
 *
 * The queries use viewport CSS pixels, so they follow browser zoom as well as window size.
 * Their thresholds also include the root's appearance scale: root zoom reduces the local
 * layout width without changing the viewport width reported to matchMedia.
 *
 * This hook is the single source of truth for the mode. Hiding a rail with
 * `@media (max-width: 1279px) { display: none }` would leave the gates, routines, tiles and
 * VIKI's status in the DOM with no way to reach them; a rail that is not docked is a drawer
 * the top bar can open, and a closed drawer is not rendered at all.
 *
 * With no `window.matchMedia` (jsdom implements none) both rails read as docked: every existing
 * jsdom test keeps the DOM it was written against, and a browser without the API would get the
 * widest layout rather than a window with no rails in it.
 */
import { useCallback, useMemo, useSyncExternalStore } from "react";

export const LEFT_DOCKED_QUERY = "(min-width: 800px)";
export const RIGHT_DOCKED_QUERY = "(min-width: 1280px)";

export interface LayoutMode {
  /** The left rail (sessions and agents) sits in the shell rather than behind a top-bar toggle. */
  leftDocked: boolean;
  /** The right rail (the VIKI panel) sits in the shell rather than behind a top-bar toggle. */
  rightDocked: boolean;
}

/** The root's `--font-scale` zoom, 1 when unset. */
export function rootScale(): number {
  const scale = Number(document.documentElement.style.getPropertyValue("--font-scale"));
  return Number.isFinite(scale) && scale > 0 ? scale : 1;
}

/** Calls `onStoreChange` whenever the root's inline style, and so its zoom, may have changed. */
export function subscribeRootScale(onStoreChange: () => void): () => void {
  const observer = new MutationObserver(onStoreChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["style"] });
  return () => observer.disconnect();
}

function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      if (typeof window.matchMedia !== "function") return () => {};
      const list = window.matchMedia(query);
      list.addEventListener("change", onStoreChange);
      return () => list.removeEventListener("change", onStoreChange);
    },
    [query],
  );
  // Asked afresh on every read rather than held in a ref: the snapshot is a boolean, so
  // useSyncExternalStore's identity check is satisfied either way, and nothing here can go stale
  // against a window that changed between renders.
  const getSnapshot = useCallback(
    () => (typeof window.matchMedia === "function" ? window.matchMedia(query).matches : true),
    [query],
  );
  return useSyncExternalStore(subscribe, getSnapshot);
}

/** The current mode, recomputed when either breakpoint or the root appearance scale changes. */
export function useLayoutMode(): LayoutMode {
  const scale = useSyncExternalStore(subscribeRootScale, rootScale);
  const leftDocked = useMediaQuery(
    scale === 1 ? LEFT_DOCKED_QUERY : `(min-width: ${800 * scale}px)`,
  );
  const rightDocked = useMediaQuery(
    scale === 1 ? RIGHT_DOCKED_QUERY : `(min-width: ${1280 * scale}px)`,
  );
  // One object per distinct pair of booleans, so a caller may name the whole value in an effect's
  // dependency list without that effect re-running on every unrelated render.
  return useMemo(() => ({ leftDocked, rightDocked }), [leftDocked, rightDocked]);
}
