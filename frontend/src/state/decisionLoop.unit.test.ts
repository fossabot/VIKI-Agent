/**
 * useDecisionLauncher: mount/visibility-driven reads, the 15 s heartbeat, open()'s re-check
 * racing a plain read, optimistic feedback, observe's "shown" de-dup, and unmount aborts. Every
 * `DecisionLoopApi` method used here is driven one step at a time with deferred promises — the
 * same technique usePanes.unit.test.ts uses for its own async continuations.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  type DecisionLoopApi,
  DecisionLoopApiError,
  type ItemView,
  type LauncherView,
} from "../clients/decisionLoop/types";
import { POLL_MS, type UseDecisionLauncherOptions, useDecisionLauncher } from "./decisionLoop";

function item(key: string, overrides: Partial<ItemView> = {}): ItemView {
  return {
    item_key: key,
    episode_id: `episode-${key}`,
    candidate: "tasks_due",
    kind: "suggest",
    level: "badge",
    what: `Item ${key}`,
    why: "why",
    evidence: [],
    actions: [],
    created_at: "2026-10-21T14:02:00.000Z",
    mission_id: null,
    ...overrides,
  };
}

function launcherView(overrides: Partial<LauncherView> = {}): LauncherView {
  return {
    state: "on",
    level: "list",
    count: 0,
    nudge: 0,
    glow: 0,
    working: 0,
    open: 0,
    checked_at: "2026-10-21T14:02:00.000Z",
    paused_until: null,
    focus_until: null,
    model: "ready",
    items: [],
    started: [],
    review: { due: false, count: 0 },
    plans_waiting: 0,
    ...overrides,
  };
}

interface PendingCall<Args extends unknown[], T> {
  args: Args;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

/** A `vi.fn()`-backed stand-in for one `DecisionLoopApi` method: every call is queued, answered
 * one at a time (FIFO) by a test, exactly like usePanes.unit.test.ts's `fakeSave`. */
function pendingCalls<Args extends unknown[], T>(): {
  fn: (...args: Args) => Promise<T>;
  next: () => PendingCall<Args, T>;
  count: () => number;
} {
  const queue: PendingCall<Args, T>[] = [];
  const fn = vi.fn((...args: Args): Promise<T> => {
    let resolve!: (value: T) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    queue.push({ args, resolve, reject });
    return promise;
  });
  return {
    fn,
    next: () => {
      const call = queue.shift();
      if (!call) throw new Error("no pending call to settle");
      return call;
    },
    count: () => fn.mock.calls.length,
  };
}

/** A stub for a `DecisionLoopApi` method this test never calls; it exists only so the mock
 * object satisfies the full interface. */
function unused<T>(name: string): () => Promise<T> {
  return vi.fn(() => Promise.reject(new Error(`${name} unused in this test`)));
}

function createMockApi(): {
  api: DecisionLoopApi;
  launcherQ: ReturnType<typeof pendingCalls<[AbortSignal | undefined], LauncherView>>;
  openQ: ReturnType<typeof pendingCalls<[AbortSignal | undefined], LauncherView>>;
  feedbackQ: ReturnType<typeof pendingCalls<[Record<string, unknown>], LauncherView>>;
  observeFn: ReturnType<typeof vi.fn>;
} {
  const launcherQ = pendingCalls<[AbortSignal | undefined], LauncherView>();
  const openQ = pendingCalls<[AbortSignal | undefined], LauncherView>();
  const feedbackQ = pendingCalls<[Record<string, unknown>], LauncherView>();
  const observeFn = vi.fn(() => Promise.resolve());

  const api: DecisionLoopApi = {
    launcher: launcherQ.fn,
    openLauncher: openQ.fn,
    presence: unused("presence"),
    feedback: feedbackQ.fn,
    observe: observeFn,
    report: unused("report"),
    page: unused("page"),
    preference: unused("preference"),
    deleteEpisodes: unused("deleteEpisodes"),
    saveKey: unused("saveKey"),
    removeKey: unused("removeKey"),
    test: unused("test"),
    calls: unused("calls"),
    call: unused("call"),
    missions: unused("missions"),
    startMission: unused("startMission"),
    stopMission: unused("stopMission"),
    stopAllMissions: unused("stopAllMissions"),
    startSeat: unused("startSeat"),
    stopSeat: unused("stopSeat"),
    runChecks: unused("runChecks"),
    stopChecks: unused("stopChecks"),
    suggestInbox: unused("suggestInbox"),
    createProposal: unused("createProposal"),
    proposal: unused("proposal"),
    actProposal: unused("actProposal"),
    actChange: unused("actChange"),
    dataSummary: unused("dataSummary"),
    dataRecords: unused("dataRecords"),
    dataRecord: unused("dataRecord"),
    deleteData: unused("deleteData"),
  };

  return { api, launcherQ, openQ, feedbackQ, observeFn };
}

/** Flushes microtasks without advancing fake time (vitest's own idiom for settling an already
 * resolved/rejected promise chain under fake timers; see usePanes.unit.test.ts). */
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
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("reads once on mount when visible, and applies the resolved view", async () => {
  const { api, launcherQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  expect(launcherQ.count()).toBe(1);

  const view = launcherView({ items: [item("a")] });
  launcherQ.next().resolve(view);
  await settle();
  expect(result.current.view).toEqual(view);
  expect(result.current.error).toBeNull();
});

test("no read while hidden at mount; becoming visible reads once; hiding again reads nothing", async () => {
  const { api, launcherQ } = createMockApi();
  const { result, rerender } = renderHook(
    (opts: UseDecisionLauncherOptions) => useDecisionLauncher(api, opts),
    { initialProps: { visible: false } },
  );
  expect(launcherQ.count()).toBe(0);

  rerender({ visible: true });
  expect(launcherQ.count()).toBe(1);
  const view = launcherView({ items: [item("a")] });
  launcherQ.next().resolve(view);
  await settle();
  expect(result.current.view).toEqual(view);

  rerender({ visible: false });
  await advance(POLL_MS * 2);
  expect(launcherQ.count()).toBe(1); // hidden the whole time since: no heartbeat, no re-read
});

test("polls every 15 s while visible", async () => {
  const { api, launcherQ } = createMockApi();
  renderHook(() => useDecisionLauncher(api, { visible: true }));
  expect(launcherQ.count()).toBe(1);
  launcherQ.next().resolve(launcherView());
  await settle();

  await advance(POLL_MS - 1);
  expect(launcherQ.count()).toBe(1);
  await advance(1);
  expect(launcherQ.count()).toBe(2);
  launcherQ.next().resolve(launcherView());
  await settle();

  await advance(POLL_MS);
  expect(launcherQ.count()).toBe(3);
});

test("a 15 s tick that lands while a plain read is still pending is skipped, not queued", async () => {
  const { api, launcherQ } = createMockApi();
  renderHook(() => useDecisionLauncher(api, { visible: true }));
  expect(launcherQ.count()).toBe(1); // the mount read, deliberately left pending

  await advance(POLL_MS); // the heartbeat's first tick: the mount read never resolved
  expect(launcherQ.count()).toBe(1); // skipped, not a second concurrent read

  launcherQ.next().resolve(launcherView());
  await settle();

  await advance(POLL_MS); // the heartbeat's next scheduled tick (fixed at 15s, 30s, ...)
  expect(launcherQ.count()).toBe(2);
});

test("a slow plain read started before open() never overwrites the re-checked view", async () => {
  const { api, launcherQ, openQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  expect(launcherQ.count()).toBe(1); // the mount read, left pending throughout

  act(() => {
    result.current.open();
  });
  expect(result.current.checking).toBe(true);
  expect(openQ.count()).toBe(1);

  const openedView = launcherView({ items: [item("fresh")] });
  openQ.next().resolve(openedView);
  await settle();
  expect(result.current.checking).toBe(false);
  expect(result.current.view).toEqual(openedView);

  // The mount's plain read, started before open(), finally answers — strictly older, so dropped.
  launcherQ.next().resolve(launcherView({ items: [item("stale")] }));
  await settle();
  expect(result.current.view).toEqual(openedView);
});

test("open() clears checking and records the error on failure", async () => {
  const { api, launcherQ, openQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  launcherQ.next().resolve(launcherView());
  await settle();

  act(() => {
    result.current.open();
  });
  expect(result.current.checking).toBe(true);

  openQ.next().reject(new DecisionLoopApiError("unavailable", 503));
  await settle();
  expect(result.current.checking).toBe(false);
  expect(result.current.error).toBe("unavailable");
});

test("a second open() wins: the first settling late does not clear checking out from under it", async () => {
  const { api, launcherQ, openQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  launcherQ.next().resolve(launcherView());
  await settle();

  act(() => {
    result.current.open();
  });
  const first = openQ.next();
  act(() => {
    result.current.open();
  });
  const second = openQ.next();
  expect(result.current.checking).toBe(true);

  first.resolve(launcherView({ items: [item("old")] }));
  await settle();
  expect(result.current.checking).toBe(true); // the newer open() is still pending

  const freshView = launcherView({ items: [item("new")] });
  second.resolve(freshView);
  await settle();
  expect(result.current.checking).toBe(false);
  expect(result.current.view).toEqual(freshView);
});

test("feedback hides the item at once and applies the call's own view on success", async () => {
  const { api, launcherQ, feedbackQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  launcherQ.next().resolve(launcherView({ items: [item("a"), item("b")] }));
  await settle();

  act(() => {
    result.current.feedback({ item_key: "a" }, "useful");
  });
  expect(result.current.view?.items.map((row) => row.item_key)).toEqual(["b"]);

  const call = feedbackQ.next();
  expect(call.args[0]).toEqual({
    item_key: "a",
    episode_id: null,
    value: "useful",
    reason: null,
    source: "launcher",
  });

  const served = launcherView({ items: [item("b")] });
  call.resolve(served);
  await settle();
  expect(result.current.view).toEqual(served);
});

test("feedback by episode_id matches on the item's own episode, not its key", async () => {
  const { api, launcherQ, feedbackQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  launcherQ.next().resolve(launcherView({ items: [item("a", { episode_id: "ep-a" })] }));
  await settle();

  act(() => {
    result.current.feedback({ episode_id: "ep-a" }, "missed");
  });
  expect(result.current.view?.items).toEqual([]);

  const call = feedbackQ.next();
  expect(call.args[0]).toEqual({
    item_key: null,
    episode_id: "ep-a",
    value: "missed",
    reason: null,
    source: "launcher",
  });
  call.resolve(launcherView({ items: [] }));
  await settle();
});

test("feedback puts the item back and records the error on failure", async () => {
  const { api, launcherQ, feedbackQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  const a = item("a");
  const b = item("b");
  launcherQ.next().resolve(launcherView({ items: [a, b] }));
  await settle();

  act(() => {
    result.current.feedback({ item_key: "a" }, "wrong", "wrong_topic");
  });
  expect(result.current.view?.items.map((row) => row.item_key)).toEqual(["b"]);

  feedbackQ.next().reject(new DecisionLoopApiError("unknown_item", 404));
  await settle();

  expect(result.current.error).toBe("unknown_item");
  expect(result.current.view?.items).toEqual([a, b]);
});

test("feedback's failure does not resurrect an item a fresher view has already settled", async () => {
  const { api, launcherQ, openQ, feedbackQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  const a = item("a");
  launcherQ.next().resolve(launcherView({ items: [a] }));
  await settle();

  act(() => {
    result.current.feedback({ item_key: "a" }, "wrong");
  });
  expect(result.current.view?.items).toEqual([]);

  // A re-check lands before the feedback call settles, and it still has no "a" (the server
  // already dropped it for its own reasons) — the eventual feedback failure must respect that.
  act(() => {
    result.current.open();
  });
  openQ.next().resolve(launcherView({ items: [] }));
  await settle();

  feedbackQ.next().reject(new DecisionLoopApiError("unknown_item", 404));
  await settle();
  expect(result.current.view?.items).toEqual([]);
  expect(result.current.error).toBe("unknown_item");
});

test('observe de-dupes "shown" per item until the view changes; other events are never de-duped', async () => {
  const { api, launcherQ, observeFn } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  launcherQ.next().resolve(launcherView({ items: [item("a"), item("b")] }));
  await settle();

  act(() => {
    result.current.observe("shown", ["a", "b"]);
  });
  expect(observeFn).toHaveBeenCalledTimes(1);
  expect(observeFn).toHaveBeenLastCalledWith("shown", ["a", "b"]);

  act(() => {
    result.current.observe("shown", ["a", "b"]); // fully repeated: dropped
  });
  expect(observeFn).toHaveBeenCalledTimes(1);

  act(() => {
    result.current.observe("shown", ["a", "c"]); // "a" repeats, "c" is new
  });
  expect(observeFn).toHaveBeenCalledTimes(2);
  expect(observeFn).toHaveBeenLastCalledWith("shown", ["c"]);

  act(() => {
    result.current.observe("opened", ["a"]); // a different event: never de-duped
  });
  expect(observeFn).toHaveBeenCalledTimes(3);
  expect(observeFn).toHaveBeenLastCalledWith("opened", ["a"]);

  // A fresh view resets the "shown" epoch.
  await advance(POLL_MS);
  launcherQ.next().resolve(launcherView({ items: [item("a")] }));
  await settle();

  act(() => {
    result.current.observe("shown", ["a"]);
  });
  expect(observeFn).toHaveBeenCalledTimes(4);
  expect(observeFn).toHaveBeenLastCalledWith("shown", ["a"]);
});

test("observe never throws and is never retried on failure", async () => {
  const { api, launcherQ, observeFn } = createMockApi();
  observeFn.mockRejectedValueOnce(new DecisionLoopApiError("unavailable", 503));
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  launcherQ.next().resolve(launcherView({ items: [item("a")] }));
  await settle();

  expect(() => {
    act(() => {
      result.current.observe("shown", ["a"]);
    });
  }).not.toThrow();
  await settle();
  expect(observeFn).toHaveBeenCalledTimes(1);
  expect(result.current.error).toBeNull(); // a failed observation is not surfaced as `error`

  act(() => {
    result.current.observe("opened", ["a"]); // no retry of the failed "shown" call either
  });
  expect(observeFn).toHaveBeenCalledTimes(2);
});

test("refresh() forces a plain read, folded into the same in-flight guard", async () => {
  const { api, launcherQ } = createMockApi();
  const { result } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  launcherQ.next().resolve(launcherView());
  await settle();
  expect(launcherQ.count()).toBe(1);

  act(() => {
    result.current.refresh();
  });
  expect(launcherQ.count()).toBe(2);
  const refreshed = launcherView({ items: [item("x")] });
  launcherQ.next().resolve(refreshed);
  await settle();
  expect(result.current.view).toEqual(refreshed);

  // refresh() while a read is already in flight does not start a second one.
  act(() => {
    result.current.refresh();
  });
  expect(launcherQ.count()).toBe(3);
  act(() => {
    result.current.refresh();
  });
  expect(launcherQ.count()).toBe(3);
});

test("unmount aborts every outstanding plain read and open() call; their late answers are dropped", async () => {
  const { api, launcherQ, openQ } = createMockApi();
  const { result, unmount } = renderHook(() => useDecisionLauncher(api, { visible: true }));
  expect(launcherQ.count()).toBe(1);
  const mountCall = launcherQ.next();

  act(() => {
    result.current.open();
  });
  const openCall = openQ.next();

  expect(mountCall.args[0]?.aborted).toBe(false);
  expect(openCall.args[0]?.aborted).toBe(false);

  unmount();

  expect(mountCall.args[0]?.aborted).toBe(true);
  expect(openCall.args[0]?.aborted).toBe(true);

  expect(() => {
    mountCall.resolve(launcherView({ items: [item("late")] }));
    openCall.resolve(launcherView({ items: [item("late2")] }));
  }).not.toThrow();
  await settle();
  expect(result.current.view).toBeNull();
});
