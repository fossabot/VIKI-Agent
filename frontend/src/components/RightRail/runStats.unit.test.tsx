import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { RunTranscript, SubagentsApi } from "../../clients/conversations/subagentsApi";
import { ROW_REFRESH_MS, useRunStats } from "./runStats";

interface RunRef {
  key: string;
  sessionId: string;
  blockId: string;
}

interface Deferred {
  resolve: (value: RunTranscript) => void;
  reject: (error: unknown) => void;
}

interface Call {
  sessionId: string;
  blockId: string;
  signal: AbortSignal | undefined;
}

interface Props {
  runs: RunRef[];
  nowMs: number;
}

function transcript(overrides: Partial<RunTranscript> = {}): RunTranscript {
  return {
    prompt: "",
    lines: [],
    omitted: 0,
    partial: false,
    startedAt: null,
    tokens: null,
    ...overrides,
  };
}

function fakeApi(): { api: SubagentsApi; calls: Call[]; pending: Deferred[] } {
  const calls: Call[] = [];
  const pending: Deferred[] = [];
  const api: SubagentsApi = {
    read(sessionId, blockId, signal) {
      calls.push({ sessionId, blockId, signal });
      return new Promise<RunTranscript>((resolve, reject) => pending.push({ resolve, reject }));
    },
  };
  return { api, calls, pending };
}

/** Renders the hook behind a tiny wrapper so every test shares the same typed rerender(). */
function renderStats(api: SubagentsApi | undefined, initialProps: Props) {
  return renderHook(({ runs, nowMs }: Props) => useRunStats(api, runs, nowMs), { initialProps });
}

const run1: RunRef = {
  key: "s1:b1",
  sessionId: "11111111-1111-1111-1111-111111111111",
  blockId: "b1",
};
const run2: RunRef = {
  key: "s2:b2",
  sessionId: "22222222-2222-2222-2222-222222222222",
  blockId: "b2",
};

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

test("only the first run is read while nothing has settled yet", async () => {
  const { api, calls } = fakeApi();
  renderStats(api, { runs: [run1, run2], nowMs: 0 });
  await settle();

  expect(calls).toHaveLength(1);
  expect(calls[0]).toMatchObject({ sessionId: run1.sessionId, blockId: run1.blockId });
});

test("the second run is read once the first settles", async () => {
  const { api, calls, pending } = fakeApi();
  const { result } = renderStats(api, { runs: [run1, run2], nowMs: 0 });
  await settle();
  pending[0]?.resolve(transcript({ startedAt: 100, tokens: 5 }));
  await settle();

  expect(calls).toHaveLength(2);
  expect(calls[1]).toMatchObject({ sessionId: run2.sessionId, blockId: run2.blockId });
  expect(result.current.get(run1.key)).toEqual({ startedAt: 100, tokens: 5, at: 0 });
});

test("a run is not re-read before ROW_REFRESH_MS, and is re-read once it elapses", async () => {
  const { api, calls, pending } = fakeApi();
  const { rerender } = renderStats(api, { runs: [run1], nowMs: 0 });
  await settle();
  pending[0]?.resolve(transcript({ startedAt: 1, tokens: 1 }));
  await settle();
  expect(calls).toHaveLength(1);

  rerender({ runs: [run1], nowMs: ROW_REFRESH_MS - 1 });
  await settle();
  expect(calls).toHaveLength(1);

  rerender({ runs: [run1], nowMs: ROW_REFRESH_MS });
  await settle();
  expect(calls).toHaveLength(2);
});

test("a failed read keeps earlier values, stamps `at`, and is retried after ROW_REFRESH_MS", async () => {
  const { api, calls, pending } = fakeApi();
  const { result, rerender } = renderStats(api, { runs: [run1], nowMs: 0 });
  await settle();
  pending[0]?.resolve(transcript({ startedAt: 7, tokens: 2 }));
  await settle();

  rerender({ runs: [run1], nowMs: ROW_REFRESH_MS });
  await settle();
  expect(calls).toHaveLength(2);
  pending[1]?.reject(new Error("unavailable"));
  await settle();

  expect(result.current.get(run1.key)).toEqual({ startedAt: 7, tokens: 2, at: ROW_REFRESH_MS });

  rerender({ runs: [run1], nowMs: ROW_REFRESH_MS * 2 });
  await settle();
  expect(calls).toHaveLength(3);
});

test("a first read that fails stores nulls rather than leaving no entry", async () => {
  const { api, pending } = fakeApi();
  const { result } = renderStats(api, { runs: [run1], nowMs: 5 });
  await settle();
  pending[0]?.reject(new Error("unavailable"));
  await settle();

  expect(result.current.get(run1.key)).toEqual({ startedAt: null, tokens: null, at: 5 });
});

test("a clock tick while the first read is still in flight starts no second read", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderStats(api, { runs: [run1], nowMs: 0 });
  await settle();
  expect(calls).toHaveLength(1);

  for (const nowMs of [1000, 2000, 3000]) {
    rerender({ runs: [run1], nowMs });
    await settle();
  }

  expect(calls).toHaveLength(1);
});

test("unmounting aborts the in-flight read", async () => {
  const { api, calls } = fakeApi();
  const { unmount } = renderStats(api, { runs: [run1], nowMs: 0 });
  await settle();
  expect(calls[0]?.signal?.aborted).toBe(false);

  unmount();

  expect(calls[0]?.signal?.aborted).toBe(true);
});

test("with no api, nothing is read and the map stays empty", async () => {
  const { result } = renderStats(undefined, { runs: [run1], nowMs: 0 });
  await settle();

  expect(result.current.size).toBe(0);
});

test("a run that left the list keeps its entry, unread", async () => {
  const { api, calls, pending } = fakeApi();
  const { result, rerender } = renderStats(api, { runs: [run1, run2], nowMs: 0 });
  await settle();
  pending[0]?.resolve(transcript({ startedAt: 1, tokens: 1 }));
  await settle();
  const callsAfterFirst = calls.length;

  rerender({ runs: [run2], nowMs: 0 });
  await settle();

  expect(result.current.get(run1.key)).toEqual({ startedAt: 1, tokens: 1, at: 0 });
  // run1 is gone from `runs`; only run2 (already in flight, or freshly started) may be read.
  expect(calls.slice(callsAfterFirst).every((call) => call.sessionId === run2.sessionId)).toBe(
    true,
  );
});
