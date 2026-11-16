/**
 * useVaultOverview: a fake overview method whose call returns a controllable (deferred) promise,
 * driven one step at a time with fake timers — the same technique useCaptureListing.unit.test.ts
 * uses for its own debounced effect.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { VaultOverview } from "./vaultApi";
import { useVaultOverview } from "./vaultOverview";

interface Call {
  workspaceId: string;
  signal: AbortSignal | undefined;
}
interface Deferred<T> {
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function fakeApi(): {
  api: { overview(id: string, signal?: AbortSignal): Promise<VaultOverview> };
  calls: Call[];
  deferred: Deferred<VaultOverview>[];
} {
  const calls: Call[] = [];
  const deferred: Deferred<VaultOverview>[] = [];
  const api = {
    overview(id: string, signal?: AbortSignal) {
      calls.push({ workspaceId: id, signal });
      return new Promise<VaultOverview>((resolve, reject) => deferred.push({ resolve, reject }));
    },
  };
  return { api, calls, deferred };
}

function overview(changes: Partial<VaultOverview> = {}): VaultOverview {
  return { notes: [], partial: false, partial_reasons: [], omitted: 0, read_bytes: 0, ...changes };
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

describe("useVaultOverview", () => {
  it("without an overview method, stays at phase none and calls nothing", async () => {
    // A stable reference, as a real caller's `api` object would be: a fresh `{}` on every
    // render would retrigger the effect every time (same constraint as VaultBrowser's own list
    // effect, which is equally unconditional about the state object it builds each run).
    const noOverview = {};
    const { result } = renderHook(() => useVaultOverview(noOverview, "w1", 0));
    expect(result.current.phase).toBe("none");
    expect(result.current.data).toBeNull();
    await advance(1000);
    expect(result.current.phase).toBe("none");
  });

  it("the mount run calls on the next tick, then resolves to ready", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result } = renderHook(() => useVaultOverview(api, "w1", 0));
    expect(result.current.phase).toBe("loading");
    expect(calls).toHaveLength(0);

    await settle();
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ workspaceId: "w1" });
    expect(calls[0]?.signal?.aborted).toBe(false);

    deferred[0]?.resolve(overview({ read_bytes: 10 }));
    await settle();
    expect(result.current.phase).toBe("ready");
    expect(result.current.data).toEqual(overview({ read_bytes: 10 }));
    expect(result.current.error).toBe("");
  });

  it("a second mount with the same api starts with the remembered data and calls once", async () => {
    const { api, calls, deferred } = fakeApi();
    const first = renderHook(() => useVaultOverview(api, "w1", 0));
    await settle();
    deferred[0]?.resolve(overview({ read_bytes: 5 }));
    await settle();
    expect(first.result.current.phase).toBe("ready");
    first.unmount();

    const second = renderHook(() => useVaultOverview(api, "w1", 0));
    // Seeded synchronously from the WeakMap, before the fresh request below has landed — this
    // assertion fails without it, since a plain `data: null` initial state would show nothing.
    expect(second.result.current.phase).toBe("loading");
    expect(second.result.current.data).toEqual(overview({ read_bytes: 5 }));
    expect(calls).toHaveLength(1);

    await settle();
    expect(calls).toHaveLength(2);
    deferred[1]?.resolve(overview({ read_bytes: 6 }));
    await settle();
    expect(second.result.current.phase).toBe("ready");
    expect(second.result.current.data).toEqual(overview({ read_bytes: 6 }));
  });

  it("a different api, or the same api with a different workspace, starts empty", async () => {
    const { api, deferred } = fakeApi();
    const first = renderHook(() => useVaultOverview(api, "w1", 0));
    await settle();
    deferred[0]?.resolve(overview({ read_bytes: 9 }));
    await settle();
    first.unmount();

    const otherApi = fakeApi();
    const freshClient = renderHook(() => useVaultOverview(otherApi.api, "w1", 0));
    expect(freshClient.result.current.data).toBeNull();
    freshClient.unmount();

    const otherWorkspace = renderHook(() => useVaultOverview(api, "w2", 0));
    expect(otherWorkspace.result.current.data).toBeNull();
    otherWorkspace.unmount();
  });

  it("under StrictMode's development remount, a mount still sends exactly one request", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result } = renderHook(() => useVaultOverview(api, "w1", 0), { wrapper: StrictMode });
    await settle();
    expect(calls).toHaveLength(1);

    deferred[0]?.resolve(overview({ read_bytes: 3 }));
    await settle();
    expect(result.current.phase).toBe("ready");
  });

  it("a rejected call sets phase error with the Error's message", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result } = renderHook(() => useVaultOverview(api, "w1", 0));
    await advance(250);
    deferred[0]?.reject(new Error("not_found"));
    await settle();
    expect(result.current.phase).toBe("error");
    expect(result.current.error).toBe("not_found");
    expect(result.current.data).toBeNull();

    // A non-overloaded error does not retry.
    await advance(1000);
    expect(calls).toHaveLength(1);
  });

  it("a non-Error rejection reports unavailable", async () => {
    const { api, deferred } = fakeApi();
    const { result } = renderHook(() => useVaultOverview(api, "w1", 0));
    await advance(250);
    deferred[0]?.reject("boom");
    await settle();
    expect(result.current.phase).toBe("error");
    expect(result.current.error).toBe("unavailable");
  });

  it("unmounting before the timer fires cancels it; no call is ever made", async () => {
    const { api, calls } = fakeApi();
    // The mount run's timer is due at the next tick (0ms): it must be cancelled synchronously,
    // before any time at all has passed, or it has already fired.
    const { unmount } = renderHook(() => useVaultOverview(api, "w1", 0));
    unmount();
    await advance(1000);
    expect(calls).toHaveLength(0);
  });

  it("unmounting after the call started aborts its signal", async () => {
    const { api, calls } = fakeApi();
    const { unmount } = renderHook(() => useVaultOverview(api, "w1", 0));
    await advance(250);
    expect(calls[0]?.signal?.aborted).toBe(false);

    unmount();
    expect(calls[0]?.signal?.aborted).toBe(true);
  });

  it("changing the workspace aborts the previous call, clears data and starts a fresh one", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result, rerender } = renderHook(({ id }) => useVaultOverview(api, id, 0), {
      initialProps: { id: "w1" },
    });
    await advance(250);
    deferred[0]?.resolve(overview({ read_bytes: 1 }));
    await settle();
    expect(result.current.data).toEqual(overview({ read_bytes: 1 }));

    rerender({ id: "w2" });
    expect(calls[0]?.signal?.aborted).toBe(true);
    expect(result.current.phase).toBe("loading");
    expect(result.current.data).toBeNull();

    await advance(250);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ workspaceId: "w2" });
    deferred[1]?.resolve(overview({ read_bytes: 2 }));
    await settle();
    expect(result.current.phase).toBe("ready");
    expect(result.current.data).toEqual(overview({ read_bytes: 2 }));
  });

  it("a stale answer from a workspace the hook has left is dropped", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result, rerender } = renderHook(({ id }) => useVaultOverview(api, id, 0), {
      initialProps: { id: "w1" },
    });
    await advance(250);
    rerender({ id: "w2" });
    await advance(250);
    expect(calls).toHaveLength(2);

    // The late w1 answer must not resurrect state for a workspace the hook has left.
    deferred[0]?.resolve(overview({ read_bytes: 999 }));
    await settle();
    expect(result.current.data).toBeNull();
    expect(result.current.phase).toBe("loading");

    deferred[1]?.resolve(overview({ read_bytes: 2 }));
    await settle();
    expect(result.current.phase).toBe("ready");
    expect(result.current.data).toEqual(overview({ read_bytes: 2 }));
  });

  it("refreshing the same owner keeps the previous data visible while loading", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result, rerender } = renderHook(({ gen }) => useVaultOverview(api, "w1", gen), {
      initialProps: { gen: 0 },
    });
    await advance(250);
    deferred[0]?.resolve(overview({ read_bytes: 1 }));
    await settle();
    expect(result.current.phase).toBe("ready");

    rerender({ gen: 1 });
    expect(result.current.phase).toBe("loading");
    expect(result.current.data).toEqual(overview({ read_bytes: 1 }));

    await advance(250);
    expect(calls).toHaveLength(2);
    deferred[1]?.resolve(overview({ read_bytes: 2 }));
    await settle();
    expect(result.current.phase).toBe("ready");
    expect(result.current.data).toEqual(overview({ read_bytes: 2 }));
  });

  it("an error on a refresh keeps the last data, with phase error", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result, rerender } = renderHook(({ gen }) => useVaultOverview(api, "w1", gen), {
      initialProps: { gen: 0 },
    });
    await settle();
    deferred[0]?.resolve(overview({ read_bytes: 4 }));
    await settle();
    expect(result.current.phase).toBe("ready");

    rerender({ gen: 1 });
    await advance(250);
    expect(calls).toHaveLength(2);
    deferred[1]?.reject(new Error("not_found"));
    await settle();
    expect(result.current.phase).toBe("error");
    expect(result.current.error).toBe("not_found");
    expect(result.current.data).toEqual(overview({ read_bytes: 4 }));
  });

  it("retries once on overloaded, then succeeds", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result } = renderHook(() => useVaultOverview(api, "w1", 0));
    await advance(250);
    expect(calls).toHaveLength(1);

    deferred[0]?.reject(new Error("overloaded"));
    await settle();
    expect(result.current.phase).toBe("loading");

    await advance(999);
    expect(calls).toHaveLength(1);
    await advance(1);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ workspaceId: "w1" });

    deferred[1]?.resolve(overview({ read_bytes: 7 }));
    await settle();
    expect(result.current.phase).toBe("ready");
    expect(result.current.data).toEqual(overview({ read_bytes: 7 }));
  });

  it("four overloaded answers in a row end in error after exactly four calls", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result } = renderHook(() => useVaultOverview(api, "w1", 0));

    await advance(250);
    expect(calls).toHaveLength(1);
    deferred[0]?.reject(new Error("overloaded"));
    await settle();

    await advance(1000);
    expect(calls).toHaveLength(2);
    deferred[1]?.reject(new Error("overloaded"));
    await settle();
    expect(result.current.phase).toBe("loading");

    await advance(1000);
    expect(calls).toHaveLength(3);
    deferred[2]?.reject(new Error("overloaded"));
    await settle();
    expect(result.current.phase).toBe("loading");

    await advance(1000);
    expect(calls).toHaveLength(4);
    deferred[3]?.reject(new Error("overloaded"));
    await settle();

    expect(result.current.phase).toBe("error");
    expect(result.current.error).toBe("overloaded");
    await advance(1000);
    expect(calls).toHaveLength(4);
  });

  it("unmounting during the retry wait cancels it; no further call is made", async () => {
    const { api, calls, deferred } = fakeApi();
    const { unmount } = renderHook(() => useVaultOverview(api, "w1", 0));
    await advance(250);
    deferred[0]?.reject(new Error("overloaded"));
    await settle();

    await advance(300);
    unmount();
    await advance(1000);
    expect(calls).toHaveLength(1);
  });

  it("changing the workspace during a retry wait starts the new workspace's read and drops the old retry", async () => {
    const { api, calls, deferred } = fakeApi();
    const { result, rerender } = renderHook(({ id }) => useVaultOverview(api, id, 0), {
      initialProps: { id: "w1" },
    });
    await advance(250);
    deferred[0]?.reject(new Error("overloaded"));
    await settle();
    expect(result.current.phase).toBe("loading");

    rerender({ id: "w2" });
    expect(result.current.data).toBeNull();

    // Past both the new workspace's 250ms debounce and the dropped retry's 1,000ms wait.
    await advance(1000);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toMatchObject({ workspaceId: "w2" });

    deferred[1]?.resolve(overview({ read_bytes: 3 }));
    await settle();
    expect(result.current.phase).toBe("ready");
    expect(result.current.data).toEqual(overview({ read_bytes: 3 }));
  });
});
