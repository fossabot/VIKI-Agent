import { act, renderHook } from "@testing-library/react";
import { StrictMode } from "react";
import { expect, test } from "vitest";
import type { OpenRequest, RunsApi } from "../../clients/conversations/runsApi";
import type { Card, SessionView } from "../../state/view";
import { emptyView } from "../../state/view";
import { useRunRequests } from "./runRequests";

interface Deferred {
  resolve: (value: OpenRequest[]) => void;
  reject: (error: unknown) => void;
}

interface Call {
  sessionId: string;
  signal: AbortSignal | undefined;
}

interface Props {
  sessions: Readonly<Record<string, SessionView>>;
}

function fakeApi(): { api: RunsApi; calls: Call[]; pending: Deferred[] } {
  const calls: Call[] = [];
  const pending: Deferred[] = [];
  const api: RunsApi = {
    requests(sessionId, signal) {
      calls.push({ sessionId, signal });
      return new Promise<OpenRequest[]>((resolve, reject) => pending.push({ resolve, reject }));
    },
    async stop() {
      throw new Error("stop() is not used by useRunRequests");
    },
  };
  return { api, calls, pending };
}

function permissionCard(toolUseId: string): Card {
  return {
    kind: "permission",
    data: {
      tool_use_id: toolUseId,
      kind: "tool",
      tool: "Write",
      summary: "",
      question: "",
      reason: "",
      agent: "",
      plan: "",
    },
    nativeRequest: null,
  };
}

function openRequest(toolUseId: string, overrides: Partial<OpenRequest> = {}): OpenRequest {
  return { toolUseId, run: null, raisedAt: 0, ...overrides };
}

function session(sessionId: string, cards: Card[]): SessionView {
  return { ...emptyView(sessionId), cards };
}

/** Renders the hook behind a tiny wrapper so every test shares the same typed rerender(). */
function renderRunRequests(api: RunsApi | undefined, initialProps: Props) {
  return renderHook(({ sessions }: Props) => useRunRequests(api, sessions), { initialProps });
}

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

test("a chat with a card is read once, by its session id", async () => {
  const { api, calls } = fakeApi();
  renderRunRequests(api, { sessions: { s1: session("s1", [permissionCard("tu-1")]) } });
  await settle();

  expect(calls).toHaveLength(1);
  expect(calls[0]).toMatchObject({ sessionId: "s1" });
});

test("two chats each with a card are read independently", async () => {
  const { api, calls } = fakeApi();
  renderRunRequests(api, {
    sessions: {
      s1: session("s1", [permissionCard("tu-1")]),
      s2: session("s2", [permissionCard("tu-2")]),
    },
  });
  await settle();

  expect(calls.map((call) => call.sessionId).sort()).toEqual(["s1", "s2"]);
});

test("an unchanged signature across a rerender reads no more", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1")]) },
  });
  await settle();
  expect(calls).toHaveLength(1);

  rerender({ sessions: { s1: session("s1", [permissionCard("tu-1")]) } });
  await settle();

  expect(calls).toHaveLength(1);
});

test("a changed signature aborts the earlier read and starts a new one", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1")]) },
  });
  await settle();
  expect(calls[0]?.signal?.aborted).toBe(false);

  rerender({
    sessions: { s1: session("s1", [permissionCard("tu-1"), permissionCard("tu-2")]) },
  });
  await settle();

  expect(calls[0]?.signal?.aborted).toBe(true);
  expect(calls).toHaveLength(2);
  expect(calls[1]?.signal?.aborted).toBe(false);
});

test("reordering the same cards changes the signature and re-reads", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1"), permissionCard("tu-2")]) },
  });
  await settle();
  expect(calls).toHaveLength(1);

  rerender({
    sessions: { s1: session("s1", [permissionCard("tu-2"), permissionCard("tu-1")]) },
  });
  await settle();

  expect(calls[0]?.signal?.aborted).toBe(true);
  expect(calls).toHaveLength(2);
});

test("a success keeps only the entries whose card is still current", async () => {
  const { api, pending } = fakeApi();
  const { result } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1"), permissionCard("tu-2")]) },
  });
  await settle();
  pending[0]?.resolve([
    openRequest("tu-1", { run: "r1", raisedAt: 10 }),
    openRequest("tu-stale", { run: "r9", raisedAt: 20 }),
  ]);
  await settle();

  const entries = result.current.get("s1");
  expect(entries && Array.from(entries.keys())).toEqual(["tu-1"]);
  expect(entries?.get("tu-1")).toEqual({ run: "r1", raisedAt: 10 });
});

test("a failed read keeps the chat's previous entries", async () => {
  const { api, pending } = fakeApi();
  const { result, rerender } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1")]) },
  });
  await settle();
  pending[0]?.resolve([openRequest("tu-1", { run: "r1", raisedAt: 5 })]);
  await settle();
  expect(result.current.get("s1")?.get("tu-1")).toEqual({ run: "r1", raisedAt: 5 });

  rerender({
    sessions: { s1: session("s1", [permissionCard("tu-1"), permissionCard("tu-2")]) },
  });
  await settle();
  pending[1]?.reject(new Error("unavailable"));
  await settle();

  expect(result.current.get("s1")?.get("tu-1")).toEqual({ run: "r1", raisedAt: 5 });
  expect(result.current.get("s1")?.has("tu-2")).toBe(false);
});

test("a chat whose cards empty before its read settles aborts that read", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1")]) },
  });
  await settle();
  expect(calls[0]?.signal?.aborted).toBe(false);

  rerender({ sessions: { s1: session("s1", []) } });
  await settle();

  expect(calls[0]?.signal?.aborted).toBe(true);
  expect(calls).toHaveLength(1);
});

test("a chat that already has entries loses them once its cards empty", async () => {
  const { api, pending } = fakeApi();
  const { result, rerender } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1")]) },
  });
  await settle();
  pending[0]?.resolve([openRequest("tu-1")]);
  await settle();
  expect(result.current.has("s1")).toBe(true);

  rerender({ sessions: { s1: session("s1", []) } });
  await settle();

  expect(result.current.has("s1")).toBe(false);
});

test("with no api, nothing is read and the map stays empty", async () => {
  const { result } = renderRunRequests(undefined, {
    sessions: { s1: session("s1", [permissionCard("tu-1")]) },
  });
  await settle();

  expect(result.current.size).toBe(0);
});

test("unmounting aborts every chat's in-flight read", async () => {
  const { api, calls } = fakeApi();
  const { unmount } = renderRunRequests(api, {
    sessions: {
      s1: session("s1", [permissionCard("tu-1")]),
      s2: session("s2", [permissionCard("tu-2")]),
    },
  });
  await settle();
  expect(calls).toHaveLength(2);
  expect(calls.every((call) => call.signal?.aborted === false)).toBe(true);

  unmount();

  expect(calls.every((call) => call.signal?.aborted === true)).toBe(true);
});

test("the returned map keeps its identity across a rerender that changes nothing", async () => {
  const { api, pending } = fakeApi();
  const { result, rerender } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1")]) },
  });
  await settle();
  pending[0]?.resolve([openRequest("tu-1")]);
  await settle();
  const before = result.current;

  rerender({ sessions: { s1: session("s1", [permissionCard("tu-1")]) } });
  await settle();

  expect(result.current).toBe(before);
});

test("under StrictMode's development-only remount, only the surviving read commits", async () => {
  const { api, calls, pending } = fakeApi();
  const { result } = renderHook(({ sessions }: Props) => useRunRequests(api, sessions), {
    initialProps: { sessions: { s1: session("s1", [permissionCard("tu-1")]) } },
    wrapper: StrictMode,
  });
  await settle();

  expect(calls).toHaveLength(2);
  expect(calls[0]?.signal?.aborted).toBe(true);
  expect(calls[1]?.signal?.aborted).toBe(false);

  pending[0]?.resolve([openRequest("tu-1", { run: "discarded", raisedAt: 1 })]);
  pending[1]?.resolve([openRequest("tu-1", { run: "r1", raisedAt: 2 })]);
  await settle();

  expect(result.current.get("s1")?.get("tu-1")).toEqual({ run: "r1", raisedAt: 2 });
});

test("the newer read's success stands even when the earlier, now-aborted read settles after it", async () => {
  const { api, pending } = fakeApi();
  const { result, rerender } = renderRunRequests(api, {
    sessions: { s1: session("s1", [permissionCard("tu-1")]) },
  });
  await settle();

  // The signature changes: the first read (pending[0]) is aborted, a second (pending[1]) starts.
  rerender({
    sessions: { s1: session("s1", [permissionCard("tu-1"), permissionCard("tu-2")]) },
  });
  await settle();

  // The newer, live read settles first — the ordinary case.
  pending[1]?.resolve([openRequest("tu-1", { run: "r-new", raisedAt: 2 })]);
  await settle();
  expect(result.current.get("s1")?.get("tu-1")).toEqual({ run: "r-new", raisedAt: 2 });

  // The earlier, now-aborted read settles only afterwards, with different data: the guard
  // (`if (controller.signal.aborted) return;`) must keep it from landing over the newer entries.
  pending[0]?.resolve([openRequest("tu-1", { run: "r-old", raisedAt: 1 })]);
  await settle();
  expect(result.current.get("s1")?.get("tu-1")).toEqual({ run: "r-new", raisedAt: 2 });
});
