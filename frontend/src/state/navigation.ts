import type { Navigation, View } from "../commands";

export const NAVIGATION_HISTORY_MAX = 40;

export interface NavigationContext {
  readonly navigation: Readonly<Navigation>;
  readonly routineId: string | null;
  readonly botId?: string | null;
  readonly note: { readonly workspace_id: string; readonly ref: string } | null;
  readonly opener: HTMLElement | null;
  readonly scroll: { readonly top: number; readonly left: number };
}

export type NavigationHistory = readonly NavigationContext[];
export type BackTarget =
  | { readonly context: NavigationContext; readonly reason: null }
  | { readonly context: null; readonly reason: "recovery_root" | "empty_history" };

/** Admission belongs to the caller; only an accepted different destination records its origin. */
export function recordTransition(
  history: NavigationHistory,
  previous: NavigationContext,
  next: NavigationContext,
  accepted: boolean,
): NavigationHistory {
  if (!accepted) return history;
  const prior = previous.navigation;
  const target = next.navigation;
  const sameDestination =
    prior.view === target.view &&
    (prior.view !== "settings" || prior.page === target.page) &&
    (prior.view !== "data" || prior.panel === target.panel) &&
    (prior.view !== "onboarding" || prior.onboardingMode === target.onboardingMode) &&
    (prior.view !== "routines" || previous.routineId === next.routineId) &&
    (prior.view !== "bots" || (previous.botId ?? null) === (next.botId ?? null)) &&
    (prior.view !== "vault" ||
      (previous.note?.workspace_id === next.note?.workspace_id &&
        previous.note?.ref === next.note?.ref));
  if (sameDestination) return history;
  if (prior.view === "recovery" || target.view === "recovery") return [];

  const snapshot: NavigationContext = {
    ...previous,
    navigation: { ...prior },
    note: previous.note ? { ...previous.note } : null,
    scroll: { ...previous.scroll },
  };
  return [...history.slice(-(NAVIGATION_HISTORY_MAX - 1)), snapshot];
}

/** A target is an in-memory snapshot; the consumer resolves its current controls and reader. */
export function peekBack(history: NavigationHistory, currentView: View): BackTarget {
  if (currentView === "recovery") return { context: null, reason: "recovery_root" };
  const context = history[history.length - 1];
  return context ? { context, reason: null } : { context: null, reason: "empty_history" };
}

/** A stale peek cannot pop another transition added while the caller checked admission. */
export function commitBack(
  history: NavigationHistory,
  currentView: View,
  expected: NavigationContext,
  accepted: boolean,
): NavigationHistory {
  if (!accepted || currentView === "recovery" || history[history.length - 1] !== expected) {
    return history;
  }
  return history.slice(0, -1);
}
