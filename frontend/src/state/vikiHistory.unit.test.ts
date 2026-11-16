import { afterEach, expect, test, vi } from "vitest";
import { FakeSocket, GENERATION } from "../accept/m3b/fakes";
import { createViki } from "./viki";

function connected() {
  const sockets: FakeSocket[] = [];
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "tok",
    createSocket: () => {
      const socket = new FakeSocket(false);
      sockets.push(socket);
      return socket;
    },
    controlTimeoutMs: 50,
  });
  viki.client.connect();
  const socket = sockets[0];
  if (!socket) throw new Error("missing socket");
  socket.open();
  return { viki, socket, sockets };
}

afterEach(() => vi.useRealTimers());

test("history leases release the last ephemeral view and ignore its late frames", () => {
  const { viki, socket } = connected();
  const id = crypto.randomUUID();
  const release = viki.actions.observeHistorySession?.(id);
  const releaseOther = viki.actions.observeHistorySession?.(id);
  expect(release).toBeTypeOf("function");
  const requestId = viki.client.subscriptionId(id);
  expect(socket.sent.at(-1)).toMatchObject({ type: "session.open", existing_only: true });
  expect(socket.sent.at(-1)).not.toHaveProperty("expected_model");
  socket.synchronize(id);
  const history = socket.sent.findLast((frame) => frame.type === "history.more");
  expect(viki.store.getSnapshot().sessions[id]?.synced).toBe(true);
  release?.();
  expect(viki.client.subscriptionId(id)).toBe(requestId);
  releaseOther?.();
  expect(viki.client.subscriptionId(id)).toBeNull();
  expect(viki.store.getSnapshot().sessions[id]).toBeUndefined();
  expect(socket.sent.at(-1)).toEqual({
    type: "session.detach",
    session_id: id,
    request_id: requestId,
  });
  const sent = socket.sent.length;
  releaseOther?.();
  expect(socket.sent).toHaveLength(sent);
  socket.receive({
    v: 2,
    type: "history.page",
    session_id: id,
    generation: GENERATION,
    request_id: history?.request_id,
    data: { events: [], next_before_uuid: null },
  });
  socket.receive({
    v: 2,
    type: "session.synced",
    session_id: id,
    generation: GENERATION,
    request_id: requestId,
    seq: 0,
    data: {},
  });
  expect(viki.store.getSnapshot().sessions[id]).toBeUndefined();
  expect(socket.sent.filter((frame) => frame.type === "session.close")).toEqual([]);
  viki.client.close();
});

test.each(["openSession", "openExistingSession"] as const)(
  "%s keeps explicit ownership after a history lease releases",
  (open) => {
    const { viki, socket } = connected();
    const id = crypto.randomUUID();
    const release = viki.actions.observeHistorySession?.(id);
    socket.synchronize(id);
    viki.actions[open](id);
    const requestId = viki.client.subscriptionId(id);
    release?.();
    expect(viki.client.subscriptionId(id)).toBe(requestId);
    expect(viki.store.getSnapshot().sessions[id]).toBeDefined();
    expect(socket.sent.filter((frame) => frame.type === "session.detach")).toEqual([]);
    viki.client.close();
  },
);

test.each([null, "viki-llm/22222222-2222-4222-8222-222222222222/example/model"])(
  "an explicit new chat keeps its model expectation when a history lease releases (%s)",
  (model) => {
    vi.useFakeTimers();
    const { viki, socket, sockets } = connected();
    const id = crypto.randomUUID();
    const release = viki.actions.observeHistorySession?.(id);
    viki.actions.newSession(id, model);
    const requestId = viki.client.subscriptionId(id);
    expect(socket.sent.at(-1)).toMatchObject({
      type: "session.open",
      session_id: id,
      expected_model: model,
      existing_only: true,
    });
    release?.();
    expect(viki.client.subscriptionId(id)).toBe(requestId);
    expect(viki.client.hasModelExpectation(id)).toBe(true);
    expect(viki.store.getSnapshot().sessions[id]).toBeDefined();
    expect(socket.sent.filter((frame) => frame.type === "session.detach")).toEqual([]);
    socket.onclose?.({ code: 1006 });
    vi.advanceTimersByTime(500);
    const replacement = sockets[1];
    if (!replacement) throw new Error("missing replacement socket");
    replacement.open();
    expect(replacement.sent.at(-1)).toMatchObject({
      type: "session.open",
      session_id: id,
      expected_model: model,
      existing_only: true,
    });
    replacement.synchronize(id);
    expect(viki.client.hasModelExpectation(id)).toBe(false);
    viki.client.close();
  },
);

test("a model refusal keeps the explicit chat paused after its history lease releases", async () => {
  vi.useFakeTimers();
  const { viki, socket, sockets } = connected();
  const id = crypto.randomUUID();
  const release = viki.actions.observeHistorySession?.(id);
  viki.actions.newSession(id, "viki-llm/22222222-2222-4222-8222-222222222222/example/model");
  const ready = viki.actions.prepareTarget(id);
  socket.receive({
    v: 2,
    type: "session.open.refused",
    session_id: id,
    request_id: viki.client.subscriptionId(id),
    generation: "",
    seq: 0,
    data: { reason: "model_review_required" },
  });
  release?.();
  expect(viki.store.getSnapshot().sessions[id]?.openRefused).toBe("model_review_required");
  expect(await ready).toBeNull();
  expect(await viki.actions.send(id, "retain this unsent request")).toBe(false);
  socket.onclose?.({ code: 1006 });
  vi.advanceTimersByTime(500);
  const replacement = sockets[1];
  if (!replacement) throw new Error("missing replacement socket");
  replacement.open();
  expect(replacement.sent.filter((frame) => frame.type === "session.open")).toEqual([]);
  viki.actions.newSession(id);
  expect(replacement.sent.at(-1)).toMatchObject({
    type: "session.open",
    session_id: id,
    existing_only: true,
  });
  expect(replacement.sent.at(-1)).not.toHaveProperty("expected_model");
  replacement.synchronize(id);
  expect(viki.store.getSnapshot().sessions[id]?.synced).toBe(true);
  expect(viki.store.getSnapshot().sessions[id]?.openRefused ?? null).toBeNull();
  expect(
    [...socket.sent, ...replacement.sent].filter(
      (frame) => frame.type === "turn.send" || frame.type === "session.close",
    ),
  ).toEqual([]);
  viki.client.close();
});

test("only retained history leases reconnect, and stale frames cannot hydrate a new lease", () => {
  vi.useFakeTimers();
  const { viki, socket, sockets } = connected();
  const removed = crypto.randomUUID();
  const kept = crypto.randomUUID();
  const releaseRemoved = viki.actions.observeHistorySession?.(removed);
  const releaseKept = viki.actions.observeHistorySession?.(kept);
  socket.synchronize(kept);
  const oldSubscription = viki.client.subscriptionId(kept);
  const oldHistory = socket.sent.findLast((frame) => frame.type === "history.more");
  releaseRemoved?.();
  socket.onclose?.({ code: 1006 });
  vi.advanceTimersByTime(500);
  const replacement = sockets[1];
  if (!replacement) throw new Error("missing replacement socket");
  replacement.open();
  expect(replacement.sent.filter((frame) => frame.type === "session.open")).toEqual([
    expect.objectContaining({ session_id: kept, existing_only: true }),
  ]);
  const currentSubscription = viki.client.subscriptionId(kept);
  expect(currentSubscription).not.toBe(oldSubscription);
  replacement.synchronize(kept);
  const before = viki.store.getSnapshot().sessions[kept];
  replacement.receive({
    v: 2,
    type: "history.page",
    session_id: kept,
    generation: GENERATION,
    request_id: oldHistory?.request_id,
    data: { events: [], next_before_uuid: null },
  });
  expect(viki.store.getSnapshot().sessions[kept]).toBe(before);
  releaseKept?.();
  const newRelease = viki.actions.observeHistorySession?.(kept);
  const newest = viki.client.subscriptionId(kept);
  releaseKept?.();
  expect(viki.client.subscriptionId(kept)).toBe(newest);
  expect(newest).not.toBe(currentSubscription);
  newRelease?.();
  viki.client.close();
});

test("releasing many historical views leaves no view or reconnect subscription", () => {
  const { viki, socket } = connected();
  const release = Array.from({ length: 200 }, () => {
    const id = crypto.randomUUID();
    return viki.actions.observeHistorySession?.(id);
  });
  expect(Object.keys(viki.store.getSnapshot().sessions)).toHaveLength(200);
  for (const stop of release) stop?.();
  expect(Object.keys(viki.store.getSnapshot().sessions)).toEqual([]);
  expect(socket.sent.filter((frame) => frame.type === "session.detach")).toHaveLength(200);
  viki.client.close();
});

test("scoped close carries its expected native turn and refuses a replacement generation", async () => {
  const { viki, socket } = connected();
  const id = viki.actions.newSession();
  socket.synchronize(id);
  const expected = { generation: GENERATION, turnId: crypto.randomUUID() };
  const result = viki.actions.closeSession(id, expected);
  const close = socket.sent.findLast((frame) => frame.type === "session.close");
  expect(close).toMatchObject({ generation: expected.generation, turn_id: expected.turnId });
  if (!close) throw new Error("missing close command");
  socket.acknowledge(close, "rejected");
  expect(await result).toBe("rejected");
  expect(viki.store.getSnapshot().sessions[id]).toBeDefined();
  socket.sent.length = 0;
  expect(
    await viki.actions.closeSession(id, { ...expected, generation: crypto.randomUUID() }),
  ).toBe("rejected");
  expect(socket.sent).toEqual([]);
  viki.actions.openExistingSession(id);
  socket.sent.length = 0;
  socket.receive({
    v: 2,
    type: "session.open.refused",
    session_id: id,
    generation: "",
    seq: 0,
    request_id: viki.client.subscriptionId(id),
    data: { reason: "unavailable" },
  });
  expect(viki.store.getSnapshot().sessions[id]?.openRefused).toBe("unavailable");
  expect(await viki.actions.closeSession(id, expected)).toBe("rejected");
  expect(viki.store.getSnapshot().sessions[id]).toBeDefined();
  expect(socket.sent).toEqual([]);
  viki.client.close();
});
