/**
 * Keeps the launcher's view fresh: `useDecisionLauncher(api, {visible})` reads GET `launcher`
 * once whenever the launcher is or becomes visible and every `POLL_MS` after that while it
 * stays visible; `open()` re-checks through POST `launcher/open` before the panel shows
 * anything, holding `checking` true until it settles; `feedback()` answers one item with source
 * "launcher"; `observe()` posts an impression/open/act best effort.
 *
 * Rules:
 * - Every plain read and every `open()` call claims the next number off one shared counter. A
 *   response only replaces `view` while its own number is still the newest one claimed, so a
 *   slow plain read started before `open()` can never overwrite the re-check it raced — the
 *   newer call's own answer always wins, whichever settles first. `checking` instead follows
 *   `open()` calls alone (its own counter), so a plain read that finishes after it never clears a
 *   still-pending re-check's flag, and an `open()` superseded by a second one never clears it out
 *   from under the newer call either.
 * - At most one plain read is ever in flight. A becoming-visible transition or a 15 s tick that
 *   lands while one is still running is simply skipped, not queued; the next trigger tries again.
 *   `open()` carries no such guard — it always runs, concurrently with whatever plain read happens
 *   to be going.
 * - `feedback()` removes the matching item from `view` (by `item_key` or `episode_id`, whichever
 *   the target names) before the call even lands, then applies the call's own answer over
 *   whatever `view` has become by the time it does. On failure, the item returns — but only if no
 *   fresher view has since settled the question either way — and the failure's code lands in
 *   `error`; a later successful read, re-check or feedback call clears it.
 * - `observe()` never throws and is never retried; a "shown" key already posted for the view
 *   currently on screen is dropped, and the de-dup resets the moment a new view replaces it.
 * - No timer runs while `visible` is false. Becoming visible (including already being visible at
 *   mount) fires one immediate read; the 15 s interval restarts counting from that same moment.
 * - Unmounting aborts every plain read and `open()` call still in flight. `feedback`/`observe`
 *   offer the wire no signal to abort; their completions are simply dropped once unmounted.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type DecisionLoopApi,
  DecisionLoopApiError,
  type FeedbackBody,
  type FeedbackReason,
  type FeedbackValue,
  type ItemView,
  type LauncherView,
  type ObserveEvent,
} from "../clients/decisionLoop/types";

/** How often the launcher re-reads while visible. */
export const POLL_MS = 15_000;

export interface UseDecisionLauncherOptions {
  /** Whether the launcher (its icon, badge or open panel) is on screen right now. The caller
   * decides this (typically from `document.visibilityState`); the hook never reads `document`
   * itself, so it needs no real DOM visibility state to test. */
  visible: boolean;
}

/** `feedback()`'s own target: a shown item by its key, or a held-back row by its episode. */
export type FeedbackTarget = { item_key: string } | { episode_id: string };

export interface UseDecisionLauncherResult {
  /** The latest applied view, or `null` before the first read lands. */
  view: LauncherView | null;
  /** The latest failure's code (`DecisionLoopApiError.code`), cleared by the next successful
   * read, re-check or feedback call. */
  error: string | null;
  /** True from an `open()` call until its own re-check settles. */
  checking: boolean;
  /** Re-checks through `launcher/open`; see the module doc. */
  open(): void;
  /** Answers one item (or held-back episode) with source "launcher"; see the module doc. */
  feedback(target: FeedbackTarget, value: FeedbackValue, reason?: FeedbackReason): void;
  /** Posts an observation best effort; never throws, never retried. */
  observe(event: ObserveEvent, keys: string[]): void;
  /** Forces a plain read now (folded with whatever plain-read discipline already applies). */
  refresh(): void;
}

function codeOf(error: unknown): string {
  return error instanceof DecisionLoopApiError ? error.code : "failed";
}

export function useDecisionLauncher(
  api: DecisionLoopApi,
  options: UseDecisionLauncherOptions,
): UseDecisionLauncherResult {
  const { visible } = options;

  const [view, setView] = useState<LauncherView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  // Mirrors `view` so `feedback()` can read the latest value synchronously, outside of React's
  // own state machinery (usePanes.ts's `layoutRef` follows the same pattern).
  const viewRef = useRef(view);
  viewRef.current = view;

  const isMountedRef = useRef(true);
  // Every plain read and every `open()` claims the next number here; see the module doc.
  const seqRef = useRef(0);
  // `open()` calls race only against each other for `checking`, never against a plain read.
  const openIdRef = useRef(0);
  const plainInFlightRef = useRef(false);
  // "shown" de-dup, reset to empty every time `view` is replaced (one epoch per view).
  const shownRef = useRef<Set<string>>(new Set());
  // Every AbortController for a plain read or an `open()` call still outstanding; unmount aborts
  // and clears all of them.
  const controllersRef = useRef<Set<AbortController>>(new Set());

  const applyView = useCallback((next: LauncherView) => {
    setView(next);
    setError(null);
    shownRef.current = new Set();
  }, []);

  const plainRead = useCallback(async (): Promise<void> => {
    if (plainInFlightRef.current) return;
    plainInFlightRef.current = true;
    const controller = new AbortController();
    controllersRef.current.add(controller);
    const mySeq = ++seqRef.current;
    try {
      const result = await api.launcher(controller.signal);
      controllersRef.current.delete(controller);
      plainInFlightRef.current = false;
      if (!isMountedRef.current || mySeq !== seqRef.current) return;
      applyView(result);
    } catch (err) {
      controllersRef.current.delete(controller);
      plainInFlightRef.current = false;
      if (!isMountedRef.current || mySeq !== seqRef.current) return;
      setError(codeOf(err));
    }
  }, [api, applyView]);

  const open = useCallback(async (): Promise<void> => {
    const id = ++openIdRef.current;
    const mySeq = ++seqRef.current;
    const controller = new AbortController();
    controllersRef.current.add(controller);
    setChecking(true);
    try {
      const result = await api.openLauncher(controller.signal);
      controllersRef.current.delete(controller);
      if (!isMountedRef.current) return;
      if (id === openIdRef.current) setChecking(false);
      if (mySeq === seqRef.current) applyView(result);
    } catch (err) {
      controllersRef.current.delete(controller);
      if (!isMountedRef.current) return;
      if (id === openIdRef.current) setChecking(false);
      if (mySeq === seqRef.current) setError(codeOf(err));
    }
  }, [api, applyView]);

  const feedback = useCallback(
    async (
      target: FeedbackTarget,
      value: FeedbackValue,
      reason?: FeedbackReason,
    ): Promise<void> => {
      const matches = (item: ItemView): boolean =>
        "item_key" in target
          ? item.item_key === target.item_key
          : item.episode_id === target.episode_id;

      // `optimistic` ties the hidden item to the exact view object it was hidden from, so the
      // failure handler below can tell "nothing has replaced this view yet" (safe to put the item
      // back) apart from "a fresher view already settled the question, present or not" (leave it
      // alone) by reference alone, never by re-deriving presence from content.
      const current = viewRef.current;
      const removedItem = current?.items.find(matches);
      const optimistic =
        current && removedItem
          ? {
              item: removedItem,
              view: { ...current, items: current.items.filter((row) => !matches(row)) },
            }
          : null;
      if (optimistic) setView(optimistic.view);

      const body: FeedbackBody =
        "item_key" in target
          ? {
              item_key: target.item_key,
              episode_id: null,
              value,
              reason: reason ?? null,
              source: "launcher",
            }
          : {
              item_key: null,
              episode_id: target.episode_id,
              value,
              reason: reason ?? null,
              source: "launcher",
            };

      try {
        const result = await api.feedback(body);
        if (!isMountedRef.current) return;
        applyView(result);
      } catch (err) {
        if (!isMountedRef.current) return;
        if (optimistic && viewRef.current === optimistic.view) {
          setView({ ...optimistic.view, items: [optimistic.item, ...optimistic.view.items] });
        }
        setError(codeOf(err));
      }
    },
    [api, applyView],
  );

  const observe = useCallback(
    (event: ObserveEvent, keys: string[]): void => {
      let toSend = keys;
      if (event === "shown") {
        const fresh = keys.filter((key) => !shownRef.current.has(key));
        for (const key of fresh) shownRef.current.add(key);
        toSend = fresh;
      }
      if (toSend.length === 0) return;
      void api.observe(event, toSend).catch(() => {});
    },
    [api],
  );

  const refresh = useCallback((): void => {
    void plainRead();
  }, [plainRead]);

  // Unmount: abort every plain read and `open()` call still outstanding.
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      for (const controller of controllersRef.current) controller.abort();
      controllersRef.current.clear();
    };
  }, []);

  // Reads once whenever the launcher is or becomes visible (mount included, when already visible
  // then); a transition to hidden fires nothing of its own.
  useEffect(() => {
    if (visible) void plainRead();
  }, [visible, plainRead]);

  // The 15 s heartbeat: only ticks while visible, and restarts counting from scratch every time
  // visibility resumes.
  useEffect(() => {
    if (!visible) return;
    const id = setInterval(() => void plainRead(), POLL_MS);
    return () => clearInterval(id);
  }, [visible, plainRead]);

  return { view, error, checking, open, feedback, observe, refresh };
}
