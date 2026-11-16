/**
 * useCaptureListing: a fake CapturesApi whose list/run/suggest calls each return a controllable
 * (deferred) promise, driven one step at a time with fake timers — the same technique
 * RunPeek.unit.test.tsx uses for its own polling hook.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  type CaptureListing,
  type CapturesApi,
  CapturesApiError,
  type SuggestionRun,
} from "../../clients/vaults/capturesApi";
import { useCaptureListing } from "./useCaptureListing";

const POLL_MS = 20; // done-when: pollMs <= 50

interface Call {
  method: "list" | "run" | "suggest";
  id: string;
  signal: AbortSignal | undefined;
}
interface Deferred<T> {
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function fakeApi(): {
  api: CapturesApi;
  calls: Call[];
  listCalls: Deferred<CaptureListing>[];
  runCalls: Deferred<SuggestionRun | null>[];
  suggestCalls: Deferred<SuggestionRun>[];
} {
  const calls: Call[] = [];
  const listCalls: Deferred<CaptureListing>[] = [];
  const runCalls: Deferred<SuggestionRun | null>[] = [];
  const suggestCalls: Deferred<SuggestionRun>[] = [];
  const api: CapturesApi = {
    list(id, signal) {
      calls.push({ method: "list", id, signal });
      return new Promise((resolve, reject) => listCalls.push({ resolve, reject }));
    },
    run(id, signal) {
      calls.push({ method: "run", id, signal });
      return new Promise((resolve, reject) => runCalls.push({ resolve, reject }));
    },
    suggest(id) {
      calls.push({ method: "suggest", id, signal: undefined });
      return new Promise((resolve, reject) => suggestCalls.push({ resolve, reject }));
    },
  };
  return { api, calls, listCalls, runCalls, suggestCalls };
}

function listing(overrides: Partial<CaptureListing> = {}): CaptureListing {
  return { captures: [], projects: [], partial: false, omitted: 0, run: null, ...overrides };
}
function run(overrides: Partial<SuggestionRun> = {}): SuggestionRun {
  return {
    run_id: "r1",
    session_id: "11111111-1111-1111-1111-111111111111",
    state: "running",
    started_at: "2026-10-09T00:00:00.000Z",
    finished_at: null,
    reason: null,
    suggested: 0,
    rejected: 0,
    ...overrides,
  };
}

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

test("mounts: calls list once and resolves into listing, clearing loading", async () => {
  const { api, calls, listCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  expect(result.current.loading).toBe(true);
  expect(calls).toHaveLength(1);
  expect(calls[0]).toMatchObject({ method: "list", id: "w1" });

  listCalls[0]?.resolve(listing());
  await settle();

  expect(result.current.loading).toBe(false);
  expect(result.current.listing).toEqual(listing());
});

test("a failed list sets error, clears loading, and leaves nothing to poll", async () => {
  const { api, listCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));

  listCalls[0]?.reject(new CapturesApiError("unavailable"));
  await settle();

  expect(result.current.loading).toBe(false);
  expect(result.current.error).toBe("unavailable");
  expect(result.current.listing).toBeNull();
});

test("changing the workspace id aborts the previous list call and starts a fresh one", async () => {
  const { api, calls, listCalls } = fakeApi();
  const { rerender } = renderHook(({ id }) => useCaptureListing(api, id, { pollMs: POLL_MS }), {
    initialProps: { id: "w1" },
  });
  expect(calls).toHaveLength(1);
  const firstSignal = calls[0]?.signal;
  expect(firstSignal?.aborted).toBe(false);

  rerender({ id: "w2" });

  expect(firstSignal?.aborted).toBe(true);
  expect(calls).toHaveLength(2);
  expect(calls[1]).toMatchObject({ method: "list", id: "w2" });

  // the stale w1 answer must not resurrect state for a workspace the hook has left.
  listCalls[0]?.resolve(listing({ omitted: 999 }));
  await settle();
  listCalls[1]?.resolve(listing());
  await settle();
  expect(calls).toHaveLength(2);
});

test("polls run every pollMs while running, one request at a time", async () => {
  const { api, calls, listCalls, runCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.resolve(listing({ run: run({ state: "running" }) }));
  await settle();
  expect(calls).toHaveLength(1);

  await advance(POLL_MS);
  expect(calls).toHaveLength(2);
  expect(calls[1]).toMatchObject({ method: "run", id: "w1" });

  // the run() call is still unresolved: no second poll fires, however long we wait.
  await advance(POLL_MS * 5);
  expect(calls).toHaveLength(2);

  runCalls[0]?.resolve(run({ state: "running" }));
  await settle();
  expect(calls).toHaveLength(2);
  expect(result.current.listing?.run?.state).toBe("running");

  await advance(POLL_MS);
  expect(calls).toHaveLength(3);
  expect(calls[2]?.method).toBe("run");
});

test("once the run ends, lists once more and stops polling for good", async () => {
  const { api, calls, listCalls, runCalls } = fakeApi();
  renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.resolve(listing({ run: run({ state: "running" }) }));
  await settle();

  await advance(POLL_MS);
  runCalls[0]?.resolve(run({ state: "done", finished_at: "2026-10-09T00:01:00.000Z" }));
  await settle();

  expect(calls).toHaveLength(3);
  expect(calls[2]?.method).toBe("list");
  listCalls[1]?.resolve(listing({ run: run({ state: "done" }) }));
  await settle();

  await advance(POLL_MS * 10);
  expect(calls).toHaveLength(3);
});

test("once the run ends, a failing follow-up list sets error and stops polling for good", async () => {
  const { api, calls, listCalls, runCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.resolve(listing({ run: run({ state: "running" }) }));
  await settle();

  await advance(POLL_MS);
  runCalls[0]?.resolve(run({ state: "done", finished_at: "2026-10-09T00:01:00.000Z" }));
  await settle();

  expect(calls).toHaveLength(3);
  expect(calls[2]?.method).toBe("list");
  listCalls[1]?.reject(new CapturesApiError("unavailable"));
  await settle();

  expect(result.current.error).toBe("unavailable");

  await advance(POLL_MS * 10);
  expect(calls).toHaveLength(3);
});

test("a failed poll sets error and stops polling", async () => {
  const { api, calls, listCalls, runCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.resolve(listing({ run: run({ state: "running" }) }));
  await settle();

  await advance(POLL_MS);
  expect(calls).toHaveLength(2);
  runCalls[0]?.reject(new CapturesApiError("unavailable"));
  await settle();

  expect(result.current.error).toBe("unavailable");

  await advance(POLL_MS * 10);
  expect(calls).toHaveLength(2);
});

test("reload clears the error and issues a fresh list", async () => {
  const { api, calls, listCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.reject(new CapturesApiError("unavailable"));
  await settle();
  expect(result.current.error).toBe("unavailable");

  act(() => {
    result.current.reload();
  });
  expect(result.current.error).toBeNull();
  expect(calls).toHaveLength(2);
  expect(calls[1]?.method).toBe("list");

  listCalls[1]?.resolve(listing());
  await settle();
  expect(result.current.listing).toEqual(listing());
  expect(result.current.loading).toBe(false);
});

test("suggest posts, merges the returned run, and starts polling", async () => {
  const { api, calls, listCalls, suggestCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.resolve(listing());
  await settle();

  act(() => {
    result.current.suggest();
  });
  expect(result.current.starting).toBe(true);
  expect(calls.at(-1)).toMatchObject({ method: "suggest", id: "w1" });

  suggestCalls[0]?.resolve(run({ state: "running" }));
  await settle();
  expect(result.current.starting).toBe(false);
  expect(result.current.listing?.run?.state).toBe("running");

  await advance(POLL_MS);
  expect(calls.at(-1)?.method).toBe("run");
});

test("a 409 suggestion_running reloads instead of surfacing startError", async () => {
  const { api, calls, listCalls, suggestCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.resolve(listing());
  await settle();

  act(() => {
    result.current.suggest();
  });
  suggestCalls[0]?.reject(new CapturesApiError("suggestion_running", { run_id: "other" }));
  await settle();

  expect(result.current.starting).toBe(false);
  expect(result.current.startError).toBeNull();
  expect(calls).toHaveLength(3);
  expect(calls[2]?.method).toBe("list");

  listCalls[1]?.resolve(listing({ run: run({ state: "running" }) }));
  await settle();
  expect(result.current.listing?.run?.state).toBe("running");
});

test("a non-409 suggest failure surfaces startError and does not reload", async () => {
  const { api, calls, listCalls, suggestCalls } = fakeApi();
  const { result } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.resolve(listing());
  await settle();

  act(() => {
    result.current.suggest();
  });
  suggestCalls[0]?.reject(new CapturesApiError("cli_missing"));
  await settle();

  expect(result.current.starting).toBe(false);
  expect(result.current.startError).toBe("cli_missing");
  expect(calls).toHaveLength(2);
});

test("switching the workspace id while a poll is in flight drops the stale answer", async () => {
  const { api, calls, listCalls, runCalls } = fakeApi();
  const { rerender, result } = renderHook(
    ({ id }) => useCaptureListing(api, id, { pollMs: POLL_MS }),
    { initialProps: { id: "w1" } },
  );
  listCalls[0]?.resolve(listing({ run: run({ state: "running" }) }));
  await settle();
  await advance(POLL_MS);
  expect(calls).toHaveLength(2);

  rerender({ id: "w2" });
  expect(calls[1]?.signal?.aborted).toBe(true);
  expect(calls).toHaveLength(3);
  expect(calls[2]).toMatchObject({ method: "list", id: "w2" });

  // the stale w1 poll answer arrives late; it must not resurrect w1's run under w2's id.
  runCalls[0]?.resolve(run({ state: "running" }));
  await settle();

  listCalls[1]?.resolve(listing({ run: null }));
  await settle();
  expect(result.current.listing?.run).toBeNull();
});

test("a suggest started before a vault switch does not land on the new workspace", async () => {
  const { api, calls, listCalls, suggestCalls } = fakeApi();
  const { rerender, result } = renderHook(
    ({ id }) => useCaptureListing(api, id, { pollMs: POLL_MS }),
    { initialProps: { id: "w1" } },
  );
  listCalls[0]?.resolve(listing());
  await settle();

  act(() => {
    result.current.suggest();
  });
  expect(result.current.starting).toBe(true);

  rerender({ id: "w2" });
  expect(result.current.starting).toBe(false);
  listCalls[1]?.resolve(listing());
  await settle();

  suggestCalls[0]?.resolve(run({ state: "running" }));
  await settle();

  expect(result.current.starting).toBe(false);
  expect(result.current.listing?.run).toBeNull();
  expect(calls.filter((call) => call.method === "run")).toHaveLength(0);
});

test("unmount aborts the in-flight list call and a late answer updates nothing", async () => {
  const { api, calls, listCalls } = fakeApi();
  const { unmount } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  expect(calls[0]?.signal?.aborted).toBe(false);

  unmount();

  expect(calls[0]?.signal?.aborted).toBe(true);
  expect(() => listCalls[0]?.resolve(listing())).not.toThrow();
  await settle();
});

test("unmount while polling clears the timer so no further request ever fires", async () => {
  const { api, calls, listCalls } = fakeApi();
  const { unmount } = renderHook(() => useCaptureListing(api, "w1", { pollMs: POLL_MS }));
  listCalls[0]?.resolve(listing({ run: run({ state: "running" }) }));
  await settle();
  expect(calls).toHaveLength(1);

  unmount();
  await advance(POLL_MS * 10);

  expect(calls).toHaveLength(1);
});
