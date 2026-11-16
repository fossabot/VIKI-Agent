/**
 * usePanes: mount (restore / fail-closed / handoff), adopt-once, and the debounced save with its
 * stale-revision retry. `save`/`refresh` are driven one step at a time with deferred promises —
 * the same technique useCaptureListing.unit.test.ts uses for its own async continuations.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type SettingsView, ShellApiError } from "../clients/app/shellApi";
import { serializeLayout } from "./paneFormat";
import {
  closeTab,
  focusGroup,
  moveTab,
  openTab,
  type PaneLayout,
  pinTab,
  resize,
  singlePane,
} from "./panes";
import {
  LAYOUT_SETTING,
  PANES_PENDING_KEY,
  PANES_STORAGE_KEY,
  type UsePanesOptions,
  usePanes,
  WINDOW_KEY,
} from "./usePanes";

const TAB_1 = "11111111-1111-1111-1111-111111111111";
const TAB_2 = "22222222-2222-2222-2222-222222222222";
const TAB_3 = "33333333-3333-3333-3333-333333333333";

interface Deferred<T> {
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}
function deferred<T>(): { promise: Promise<T>; d: Deferred<T> } {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, d: { resolve, reject } };
}

function fakeSave(): {
  save: (text: string) => Promise<unknown>;
  calls: string[];
  next: () => Deferred<unknown>;
} {
  const calls: string[] = [];
  const pending: Deferred<unknown>[] = [];
  const save = vi.fn((text: string) => {
    calls.push(text);
    const { promise, d } = deferred<unknown>();
    pending.push(d);
    return promise;
  });
  return {
    save,
    calls,
    next: () => {
      const d = pending.shift();
      if (!d) throw new Error("no pending save to settle");
      return d;
    },
  };
}

function fakeRefresh(): { refresh: () => Promise<void>; settle: () => void } {
  const pending: Deferred<void>[] = [];
  const refresh = vi.fn(() => {
    const { promise, d } = deferred<void>();
    pending.push(d);
    return promise;
  });
  return {
    refresh,
    settle: () => {
      const d = pending.shift();
      if (!d) throw new Error("no pending refresh to settle");
      d.resolve();
    },
  };
}

function settingsView(effective: Record<string, unknown>): SettingsView {
  return {
    version: 1,
    revision: 1,
    read_only: false,
    reason: null,
    values: {},
    effective,
    sources: {},
    keys: {},
  };
}

function baseOptions(overrides: Partial<UsePanesOptions> = {}): UsePanesOptions {
  return {
    enabled: true,
    handoff: false,
    settings: null,
    save: null,
    refresh: () => Promise.resolve(),
    ...overrides,
  };
}

/** Flushes microtasks without advancing fake time (vitest's own idiom for settling an already
 * resolved/rejected promise chain under fake timers). */
async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  window.sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("restores a layout with a tab from storage", () => {
  const seeded = openTab(singlePane(), TAB_1);
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(seeded));
  const { result } = renderHook(() => usePanes(baseOptions()));
  expect(result.current.restored).toBe(1);
  expect(result.current.layout).toEqual(seeded);
  expect(result.current.primary).toBe(true);
});

test("falls back to a single empty pane when storage is corrupt", () => {
  window.sessionStorage.setItem(PANES_STORAGE_KEY, "not-json-at-all");
  const { result } = renderHook(() => usePanes(baseOptions()));
  expect(result.current.restored).toBe(0);
  expect(result.current.layout).toEqual(singlePane());
});

test("a handoff window ignores storage, never adopts or saves, and stays secondary on remount", async () => {
  const seeded = openTab(singlePane(), TAB_1);
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(seeded));
  const { save } = fakeSave();
  const sv = settingsView({ [LAYOUT_SETTING]: serializeLayout(seeded) });

  const first = renderHook(() => usePanes(baseOptions({ handoff: true, settings: sv, save })));
  expect(first.result.current.primary).toBe(false);
  expect(first.result.current.restored).toBe(0);
  expect(first.result.current.layout).toEqual(singlePane());
  expect(window.sessionStorage.getItem(WINDOW_KEY)).toBe("secondary");

  await advance(5000);
  expect(save).not.toHaveBeenCalled();
  first.unmount();

  // A second handoff window, same underlying storage (now marked secondary by the first one).
  const second = renderHook(() => usePanes(baseOptions({ handoff: true })));
  expect(second.result.current.primary).toBe(false);
  expect(second.result.current.layout).toEqual(singlePane());
  expect(window.sessionStorage.getItem(WINDOW_KEY)).toBe("secondary");
});

test("adopts the setting once, when primary, enabled, and pristine", () => {
  const adopted = openTab(singlePane(), TAB_1);
  const sv = settingsView({ [LAYOUT_SETTING]: serializeLayout(adopted) });
  const { result } = renderHook(() => usePanes(baseOptions({ settings: sv })));
  expect(result.current.layout).toEqual(adopted);
  expect(result.current.restored).toBe(1);
});

test("adoption is skipped once something was restored", () => {
  const restored = openTab(singlePane(), TAB_1);
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(restored));
  const otherLayout = openTab(singlePane(), TAB_2);
  const sv = settingsView({ [LAYOUT_SETTING]: serializeLayout(otherLayout) });
  const { result } = renderHook(() => usePanes(baseOptions({ settings: sv })));
  expect(result.current.layout).toEqual(restored);
  expect(result.current.restored).toBe(1);
});

test("adoption is skipped once the layout has been touched", () => {
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions(),
  });
  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  const touched = result.current.layout;

  const adopted = openTab(singlePane(), TAB_2);
  const sv = settingsView({ [LAYOUT_SETTING]: serializeLayout(adopted) });
  rerender(baseOptions({ settings: sv }));

  expect(result.current.layout).toEqual(touched);
  expect(result.current.restored).toBe(0);
});

test("three rapid changes produce exactly one save, 1000ms after the last", async () => {
  const { save, calls } = fakeSave();
  const { result } = renderHook(() => usePanes(baseOptions({ save })));

  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  act(() => {
    result.current.update((l) => openTab(l, TAB_2));
  });
  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  const finalLayout = result.current.layout;

  await advance(999);
  expect(save).not.toHaveBeenCalled();
  await advance(1);
  expect(calls).toEqual([serializeLayout(finalLayout)]);
});

test("a caller that recreates save every render does not reset the debounce", async () => {
  const { save: trackedSave, calls } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ save: (text) => trackedSave(text) }),
  });

  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });

  // Five unrelated re-renders, each with a brand-new save closure (as an unmemoized caller
  // would produce), spread across the debounce window: none may push the deadline out, since
  // only `layout` changing is allowed to reset the timer.
  for (let i = 0; i < 5; i += 1) {
    await advance(150);
    rerender(baseOptions({ save: (text) => trackedSave(text) }));
  }
  expect(calls).toHaveLength(0); // 750ms elapsed; the deadline is 1000ms after the one update()

  await advance(250); // crosses the 1000ms mark
  expect(calls).toHaveLength(1);
});

test("a save is skipped once its text already matches the setting", async () => {
  const { save } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ save }),
  });

  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  const matching = result.current.layout;
  rerender(
    baseOptions({ save, settings: settingsView({ [LAYOUT_SETTING]: serializeLayout(matching) }) }),
  );

  await advance(1000);
  expect(save).not.toHaveBeenCalled();
});

test("a stale-revision rejection refreshes and retries once with the same text", async () => {
  const { save, calls, next } = fakeSave();
  const { refresh, settle: settleRefresh } = fakeRefresh();
  const { result } = renderHook(() => usePanes(baseOptions({ save, refresh })));

  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  await advance(1000);
  expect(calls).toHaveLength(1);

  next().reject(new ShellApiError(409, "stale_revision"));
  await settle();
  expect(refresh).toHaveBeenCalledTimes(1);

  settleRefresh();
  await settle();
  expect(calls).toHaveLength(2);
  expect(calls[1]).toBe(calls[0]);
});

test("a newer change cancels a pending retry", async () => {
  const { save, calls, next } = fakeSave();
  const { refresh, settle: settleRefresh } = fakeRefresh();
  const { result } = renderHook(() => usePanes(baseOptions({ save, refresh })));

  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  await advance(1000);
  expect(calls).toHaveLength(1);

  next().reject(new ShellApiError(409, "stale_revision"));
  await settle();
  expect(refresh).toHaveBeenCalledTimes(1);

  act(() => {
    result.current.update((l) => openTab(l, TAB_2));
  });

  settleRefresh();
  await settle();
  // The retry for the old layout was dropped; only the original failed attempt has been sent.
  expect(calls).toHaveLength(1);

  await advance(1000);
  expect(calls).toHaveLength(2);
  expect(calls[1]).toBe(serializeLayout(result.current.layout));
});

test("a non-stale-revision rejection does not refresh or retry", async () => {
  const { save, calls, next } = fakeSave();
  const { refresh } = fakeRefresh();
  const { result } = renderHook(() => usePanes(baseOptions({ save, refresh })));

  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  await advance(1000);
  expect(calls).toHaveLength(1);

  next().reject(new ShellApiError(409, "command_conflict"));
  await settle();

  expect(refresh).not.toHaveBeenCalled();
  expect(calls).toHaveLength(1);
});

test("with no save function, a change writes to sessionStorage only, never localStorage", async () => {
  const { result } = renderHook(() => usePanes(baseOptions({ save: null })));
  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  expect(window.sessionStorage.getItem(PANES_STORAGE_KEY)).toBe(
    serializeLayout(result.current.layout),
  );
  expect(window.localStorage.getItem(PANES_STORAGE_KEY)).toBeNull();
  await advance(5000);
  // No save function was supplied, so nothing above could have thrown; storage is the only sink.
});

test("two update calls in the same event both apply", () => {
  const { result } = renderHook(() => usePanes(baseOptions()));
  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
    result.current.update((l) => openTab(l, TAB_2));
  });
  // Each call must compose on the other's result, not both read the same stale render's layout
  // (that would let the second call silently overwrite the first).
  expect(result.current.layout).toEqual(openTab(openTab(singlePane(), TAB_1), TAB_2));
});

test("a no-op update does not mark the layout touched, so no save is scheduled", async () => {
  const { save } = fakeSave();
  const { result } = renderHook(() => usePanes(baseOptions({ save })));
  act(() => {
    result.current.update((l) => l);
  });
  await advance(5000);
  expect(save).not.toHaveBeenCalled();
});

test("a change written via the storage effect is restored by a fresh mount", () => {
  const first = renderHook(() => usePanes(baseOptions()));
  act(() => {
    first.result.current.update((l) => openTab(l, TAB_1));
  });
  const written = first.result.current.layout;
  first.unmount();

  const second = renderHook(() => usePanes(baseOptions()));
  expect(second.result.current.layout).toEqual(written);
  expect(second.result.current.restored).toBe(1);
});

// -- save only structural changes, save a restored change -------------------------------------

test("a window marked secondary by an earlier handoff stays non-primary on a later non-handoff mount: no adoption, no save", async () => {
  // Mark this storage secondary, exactly as a targeted window's own handoff mount does.
  const first = renderHook(() => usePanes(baseOptions({ handoff: true })));
  expect(window.sessionStorage.getItem(WINDOW_KEY)).toBe("secondary");
  first.unmount();

  const { save, calls } = fakeSave();
  const adopted = openTab(singlePane(), TAB_1);
  const sv = settingsView({ [LAYOUT_SETTING]: serializeLayout(adopted) });
  const { result } = renderHook(() =>
    usePanes(baseOptions({ handoff: false, settings: sv, save })),
  );

  // `mountState` must read `primary` from storage, not merely echo this render's `handoff` prop.
  expect(result.current.primary).toBe(false);
  expect(result.current.restored).toBe(0);
  expect(result.current.layout).toEqual(singlePane());

  act(() => {
    result.current.update((l) => openTab(l, TAB_2));
  });
  await advance(5000);
  expect(calls).toHaveLength(0);
});

test("a focus-only change after a saved layout sends nothing", async () => {
  const { save, calls } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ save }),
  });
  act(() => {
    result.current.update((l) => moveTab(openTab(l, TAB_1), TAB_2, { group: "g1", zone: "right" }));
  });
  const saved = result.current.layout;
  rerender(
    baseOptions({ save, settings: settingsView({ [LAYOUT_SETTING]: serializeLayout(saved) }) }),
  );
  await advance(1000);
  expect(calls).toHaveLength(0); // the structural change, now matching the setting, was skipped
  expect(window.sessionStorage.getItem(PANES_PENDING_KEY)).toBeNull();

  act(() => {
    result.current.update((l) => focusGroup(l, "g1"));
  });
  // `update` marks the layout dirty only on a shape change; a focus-only change never does, so
  // the pending marker stays cleared and no save is ever scheduled for it.
  expect(window.sessionStorage.getItem(PANES_PENDING_KEY)).toBeNull();
  await advance(1000);
  expect(calls).toHaveLength(0);
  expect(window.sessionStorage.getItem(PANES_PENDING_KEY)).toBeNull();
});

test("an active-only change after a saved layout sends nothing", async () => {
  const { save, calls } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ save }),
  });
  act(() => {
    result.current.update((l) => openTab(openTab(l, TAB_1), TAB_2));
  });
  const saved = result.current.layout;
  rerender(
    baseOptions({ save, settings: settingsView({ [LAYOUT_SETTING]: serializeLayout(saved) }) }),
  );
  await advance(1000);
  expect(calls).toHaveLength(0);

  act(() => {
    result.current.update((l) => openTab(l, TAB_1)); // re-selects TAB_1: active changes, nothing else
  });
  await advance(1000);
  expect(calls).toHaveLength(0);
});

const STRUCTURAL_CHANGES: ReadonlyArray<[string, (l: PaneLayout) => PaneLayout]> = [
  ["a split", (l) => moveTab(openTab(l, TAB_3), TAB_3, { group: "g2", zone: "right" })],
  ["a move", (l) => moveTab(l, TAB_1, { group: "g2", zone: "center" })],
  ["a close", (l) => closeTab(l, TAB_1)],
  ["a pin", (l) => pinTab(l, TAB_1, true)],
  ["a resize", (l) => resize(l, "s1", [0.3, 0.7])],
];

test.each(STRUCTURAL_CHANGES)(
  "a structural change (%s) sends once after the debounce with the latest full text",
  async (_label, op) => {
    const { save, calls } = fakeSave();
    // TAB_1 alone in g1, TAB_2 alone in g2, split s1, focused g2 — room for every op above.
    const base = moveTab(openTab(singlePane(), TAB_1), TAB_2, { group: "g1", zone: "right" });
    const { result } = renderHook(() =>
      usePanes(
        baseOptions({ save, settings: settingsView({ [LAYOUT_SETTING]: serializeLayout(base) }) }),
      ),
    );
    expect(result.current.layout).toEqual(base);
    await advance(1000);
    expect(calls).toHaveLength(0); // adopting `base` matches the setting it came from

    act(() => {
      result.current.update(op);
    });
    const finalLayout = result.current.layout;
    await advance(999);
    expect(calls).toHaveLength(0);
    await advance(1);
    expect(calls).toEqual([serializeLayout(finalLayout)]);
  },
);

// -- save only while a change is unsaved, mirrored by a pending marker ------------------------

// Two layouts, different enough in shape that `structureOf` never confuses one for the other:
// S0 has two groups, S1 has one group holding both tabs.
const CHURN_S0 = moveTab(openTab(singlePane(), TAB_1), TAB_2, { group: "g1", zone: "right" });
const CHURN_S1 = openTab(openTab(singlePane(), TAB_1), TAB_2);

/** App.tsx passes `settings`/`save` only while `settingsStatus === "ready"`, so a
 * `shellStore.refreshSettings()` call — another window's save landing, or any other
 * refresh — churns both null and back, with a possibly newer document when they return. */
function churnTo(
  rerender: (opts: UsePanesOptions) => void,
  save: UsePanesOptions["save"],
  sv: SettingsView,
): void {
  rerender(baseOptions({ save: null, settings: null }));
  rerender(baseOptions({ save, settings: sv }));
}

test("(a) an untouched adopted window whose save/settings churn null and back sends nothing", async () => {
  const sv0 = settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S0) });
  const { save, calls } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ settings: sv0, save }),
  });
  expect(result.current.layout).toEqual(CHURN_S0); // adopted, not restored
  expect(result.current.restored).toBe(1);
  await advance(2000);
  expect(calls).toHaveLength(0);

  churnTo(rerender, save, settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S1) }));
  await advance(2000);
  expect(calls).toHaveLength(0); // never touched: nothing was ever dirty to begin with
});

test("(b) an untouched window restored from storage, without the marker, whose save/settings churn sends nothing", async () => {
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(CHURN_S0));
  const { save, calls } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ save }),
  });
  expect(result.current.layout).toEqual(CHURN_S0);
  expect(result.current.restored).toBe(1);
  await advance(2000);
  expect(calls).toHaveLength(0);

  churnTo(rerender, save, settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S1) }));
  await advance(2000);
  expect(calls).toHaveLength(0);
});

test("(c) a reload of a stale untouched window, under a newer setting, sends nothing", async () => {
  const sv0 = settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S0) });
  const first = renderHook(() => usePanes(baseOptions({ settings: sv0 })));
  expect(first.result.current.layout).toEqual(CHURN_S0);
  first.unmount(); // closes the window: storage holds S0, the pending marker was never set

  const { save, calls } = fakeSave();
  const sv1 = settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S1) });
  const second = renderHook(() => usePanes(baseOptions({ settings: sv1, save })));
  expect(second.result.current.layout).toEqual(CHURN_S0); // restored, not re-adopted
  expect(second.result.current.restored).toBe(1);
  await advance(2000);
  expect(calls).toHaveLength(0);
});

test("(d) a reload within the debounce, marker set, sends once with the restored text", async () => {
  const first = renderHook(() => usePanes(baseOptions()));
  act(() => {
    first.result.current.update((l) => openTab(l, TAB_1));
  });
  const dirtyLayout = first.result.current.layout;
  first.unmount(); // closed mid-debounce, before any save function was even wired
  expect(window.sessionStorage.getItem(PANES_PENDING_KEY)).toBe("1");

  const { save, calls } = fakeSave();
  const second = renderHook(() => usePanes(baseOptions({ save })));
  expect(second.result.current.layout).toEqual(dirtyLayout);
  await advance(1000);
  expect(calls).toEqual([serializeLayout(dirtyLayout)]);
});

test("(e) a window that changed and saved, then sees a churned refresh bring a newer setting, sends nothing more", async () => {
  const { save, calls, next } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ save }),
  });
  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  const saved = result.current.layout;
  await advance(1000);
  expect(calls).toEqual([serializeLayout(saved)]);
  next().resolve(undefined);
  await settle();

  const otherLayout = openTab(singlePane(), TAB_2);
  churnTo(rerender, save, settingsView({ [LAYOUT_SETTING]: serializeLayout(otherLayout) }));
  await advance(2000);
  expect(calls).toHaveLength(1); // the already-saved layout is not resent
});

test("(f) a change made while a save is in flight is still saved, surviving a churn before its own debounce fires", async () => {
  const { save, calls, next } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ save }),
  });
  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  await advance(1000);
  expect(calls).toHaveLength(1);
  const firstTarget = result.current.layout;

  act(() => {
    result.current.update((l) => openTab(l, TAB_2)); // a second, real change while the first save is in flight
  });
  const secondTarget = result.current.layout;

  next().resolve(undefined); // the first save resolves; it must not clear dirty for the second change
  await settle();
  churnTo(rerender, save, settingsView({ [LAYOUT_SETTING]: serializeLayout(firstTarget) }));

  await advance(1000);
  expect(calls).toEqual([serializeLayout(firstTarget), serializeLayout(secondTarget)]);
});

test("(h) a secondary (handoff) window never reads or writes the pending marker", async () => {
  window.sessionStorage.setItem(PANES_PENDING_KEY, "1"); // belongs to some earlier primary window
  const { save } = fakeSave();
  const { result } = renderHook(() => usePanes(baseOptions({ handoff: true, save })));
  expect(result.current.primary).toBe(false);

  act(() => {
    result.current.update((l) => openTab(l, TAB_1));
  });
  await advance(5000);
  expect(save).not.toHaveBeenCalled();
  expect(window.sessionStorage.getItem(PANES_PENDING_KEY)).toBe("1"); // untouched by this window
});

test("a restored layout equal in structure to the setting is not saved", async () => {
  const restored = openTab(singlePane(), TAB_1);
  window.sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(restored));
  const sv = settingsView({ [LAYOUT_SETTING]: serializeLayout(restored) });
  const { save, calls } = fakeSave();
  const { result } = renderHook(() => usePanes(baseOptions({ settings: sv, save })));

  expect(result.current.layout).toEqual(restored);
  expect(result.current.restored).toBe(1);
  await advance(5000);
  expect(calls).toHaveLength(0);
});

// -- a focus or active change alone never marks the layout unsaved -----------------------------

test("a window whose setting has moved to another window's newer shape sends nothing on a focus-only change", async () => {
  const sv0 = settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S0) });
  const { save, calls } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ settings: sv0, save }),
  });
  expect(result.current.layout).toEqual(CHURN_S0); // adopted, nothing unsaved yet

  rerender(
    baseOptions({ save, settings: settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S1) }) }),
  );
  await advance(2000);
  expect(calls).toHaveLength(0); // the refresh alone writes nothing

  act(() => {
    result.current.update((l) => focusGroup(l, l.focused === "g1" ? "g2" : "g1"));
  });
  await advance(2000);
  expect(calls).toHaveLength(0); // the focus change never marked this window's own shape unsaved
});

test("a window whose setting has moved to another window's newer shape sends nothing on an active-only change", async () => {
  const sv0 = settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S0) });
  const { save, calls } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ settings: sv0, save }),
  });
  expect(result.current.layout).toEqual(CHURN_S0);

  rerender(
    baseOptions({ save, settings: settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S1) }) }),
  );
  await advance(2000);
  expect(calls).toHaveLength(0);

  act(() => {
    result.current.update((l) => openTab(l, TAB_1)); // re-selects TAB_1: active changes, nothing else
  });
  await advance(2000);
  expect(calls).toHaveLength(0);
});

test("a remount after a focus-only change, with the setting still on another window's newer shape, sends nothing (marker check)", async () => {
  const sv0 = settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S0) });
  const sv1 = settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S1) });
  const first = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ settings: sv0 }),
  });
  expect(first.result.current.layout).toEqual(CHURN_S0);
  first.rerender(baseOptions({ settings: sv1 })); // another window saved a different shape

  act(() => {
    first.result.current.update((l) => focusGroup(l, l.focused === "g1" ? "g2" : "g1"));
  });
  expect(window.sessionStorage.getItem(PANES_PENDING_KEY)).toBeNull(); // never marked dirty
  first.unmount(); // closes the window with nothing of its own unsaved

  const { save, calls } = fakeSave();
  const second = renderHook(() => usePanes(baseOptions({ settings: sv1, save })));
  expect(second.result.current.restored).toBe(1); // restored from storage, not re-adopted
  await advance(2000);
  expect(calls).toHaveLength(0);
});

test("a window whose setting has moved to another window's newer shape still saves once on a structural change (last writer wins)", async () => {
  const sv0 = settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S0) });
  const { save, calls } = fakeSave();
  const { result, rerender } = renderHook((opts: UsePanesOptions) => usePanes(opts), {
    initialProps: baseOptions({ settings: sv0, save }),
  });
  expect(result.current.layout).toEqual(CHURN_S0);

  rerender(
    baseOptions({ save, settings: settingsView({ [LAYOUT_SETTING]: serializeLayout(CHURN_S1) }) }),
  );
  await advance(2000);
  expect(calls).toHaveLength(0);

  act(() => {
    result.current.update((l) => closeTab(l, TAB_2)); // structural: this window's own, older shape
  });
  const finalLayout = result.current.layout;
  await advance(1000);
  expect(calls).toEqual([serializeLayout(finalLayout)]);
});
