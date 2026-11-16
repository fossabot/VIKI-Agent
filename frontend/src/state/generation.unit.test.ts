import { afterEach, expect, test, vi } from "vitest";
import { FakeSocket, GENERATION, REQUEST } from "../accept/m3b/fakes";
import type { NativeRequest, SessionSnapshot } from "../protocol";
import { liveSessionIds } from "./live";
import { createViki } from "./viki";

const NEXT = "33333333-3333-4333-8333-333333333333";
const REPLACED = "44444444-4444-4444-8444-444444444444";
const binding: NativeRequest = { runtime_id: GENERATION, request_id: REQUEST, turn_id: NEXT };
const permission = {
  tool_use_id: "tool",
  kind: "tool" as const,
  tool: "Read",
  question: "Allow?",
  summary: "synthetic",
  reason: "",
  agent: "",
  plan: "",
};
const snapshotData = (): SessionSnapshot["data"] => ({
  events: [],
  truncated: false,
  state: {
    live: false,
    info: null,
    mode: "",
    blocked: null,
    closed: false,
    error: null,
    compacting: false,
    context_percent: null,
    rate_limit: null,
  },
});

function setup() {
  const sockets: FakeSocket[] = [];
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "synthetic",
    controlTimeoutMs: 50,
    createSocket: () => {
      const socket = new FakeSocket(false);
      sockets.push(socket);
      return socket;
    },
  });
  viki.client.connect();
  const socket = sockets[0];
  if (!socket) throw new Error("missing socket");
  socket.open();
  return { viki, socket, sockets };
}
function last(socket: FakeSocket, type: string): Record<string, unknown> {
  const message = socket.sent.findLast((item) => item.type === type);
  if (!message) throw new Error(`missing ${type}`);
  return message;
}
function envelope(
  socket: FakeSocket,
  id: string,
  generation: string,
  seq: number,
  type: string,
  data: unknown,
  native_request?: NativeRequest,
) {
  const request_id = socket.sent.findLast(
    (item) => item.type === "session.open" && item.session_id === id,
  )?.request_id;
  return {
    v: 2,
    type,
    session_id: id,
    generation,
    request_id,
    seq,
    data,
    ...(native_request ? { native_request } : {}),
  };
}
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
}
afterEach(() => vi.useRealTimers());

test.each(["permission.respond", "question.answer", "session.close"])(
  "manual fixture acknowledgement for %s emits only its control receipt",
  (type) => {
    const socket = new FakeSocket(false);
    const frames: Record<string, unknown>[] = [];
    socket.onmessage = (event) => frames.push(JSON.parse(String(event.data)));
    socket.send(JSON.stringify({ type: "session.open", session_id: "a", request_id: REQUEST }));
    socket.receive({
      v: 2,
      type: "status",
      session_id: "a",
      generation: GENERATION,
      request_id: REQUEST,
      seq: 100,
      data: { state: "", mode: "" },
    });
    frames.length = 0;
    socket.acknowledge({
      type,
      session_id: "a",
      generation: GENERATION,
      command_id: NEXT,
      tool_use_id: "tool",
      native_request: binding,
    });
    expect(frames).toEqual([
      {
        v: 2,
        type: "control.receipt",
        session_id: "a",
        generation: GENERATION,
        command_id: NEXT,
        data: { status: "applied", reason: "" },
      },
    ]);
  },
);

test("new target waits for matching synchronization and a correlated local receipt", async () => {
  const { viki, socket } = setup();
  const id = viki.actions.newSession();
  const sent = viki.actions.send(id, "keep this text");
  await flush();
  expect(socket.sent.filter((item) => item.type === "turn.send")).toEqual([]);
  socket.synchronize(id);
  await flush();
  const command = last(socket, "turn.send");
  expect(command).toMatchObject({ session_id: id, generation: GENERATION, text: "keep this text" });
  let settled = false;
  void sent.then(() => {
    settled = true;
  });
  socket.receive({
    v: 2,
    type: "control.receipt",
    session_id: id,
    generation: NEXT,
    command_id: command.command_id,
    data: { status: "applied", reason: "" },
  });
  await flush();
  expect(settled).toBe(false);
  socket.acknowledge(command);
  expect(await sent).toBe(true);
  viki.client.close();
});

test("disconnect settles a waiting new-target submission and never dispatches it after reconnect", async () => {
  vi.useFakeTimers();
  const { viki, socket, sockets } = setup();
  const id = viki.actions.newSession();
  const send = viki.actions.send(id, "must not replay");
  socket.onclose?.({ code: 1006 });
  expect(await send).toBe(false);
  vi.advanceTimersByTime(500);
  const next = sockets[1];
  if (!next) throw new Error("missing reconnect");
  next.open();
  next.synchronize(id, NEXT);
  await flush();
  expect(next.sent.filter((item) => item.type === "turn.send")).toEqual([]);
  viki.client.close();
});

test.each([1, 100])(
  "replacement generation at seq %i rejects old live and subscription frames",
  async (seq) => {
    vi.useFakeTimers();
    const { viki, socket, sockets } = setup();
    const id = viki.actions.newSession();
    socket.synchronize(id, GENERATION, 5);
    const old = envelope(socket, id, GENERATION, 1000, "user.message", {
      text: "stale",
      command: "",
      at: "",
    });
    socket.onclose?.({ code: 1006 });
    vi.advanceTimersByTime(500);
    const next = sockets[1];
    if (!next) throw new Error("missing reconnect");
    next.open();
    next.synchronize(id, NEXT, seq);
    next.receive(old);
    next.receive(
      envelope(next, id, GENERATION, 1001, "user.message", {
        text: "old generation",
        command: "",
        at: "",
      }),
    );
    expect(viki.store.getSnapshot().sessions[id]?.turns).toEqual([]);
    next.receive(
      envelope(next, id, NEXT, seq + 1, "user.message", { text: "current", command: "", at: "" }),
    );
    expect(viki.store.getSnapshot().sessions[id]?.turns[0]?.user?.text).toBe("current");
    viki.client.close();
  },
);

test("history requires both the current generation and the current page request", async () => {
  vi.useFakeTimers();
  const { viki, socket, sockets } = setup();
  viki.actions.openSession("a");
  socket.synchronize("a");
  const oldHistory = last(socket, "history.more");
  socket.onclose?.({ code: 1006 });
  vi.advanceTimersByTime(500);
  const next = sockets[1];
  if (!next) throw new Error("missing reconnect");
  next.open();
  next.synchronize("a", NEXT);
  const currentHistory = last(next, "history.more");
  const page = {
    v: 2,
    type: "history.page",
    session_id: "a",
    generation: GENERATION,
    request_id: oldHistory.request_id,
    data: {
      events: [{ type: "user.message", data: { text: "old page", command: "", at: "" } }],
      next_before_uuid: null,
    },
  };
  next.receive(page);
  next.receive({ ...page, generation: NEXT });
  expect(viki.store.getSnapshot().sessions.a?.turns).toEqual([]);
  next.receive({ ...page, generation: NEXT, request_id: currentHistory.request_id });
  expect(viki.store.getSnapshot().sessions.a?.turns[0]?.user?.text).toBe("old page");
  expect(viki.store.getSnapshot().sessions.a?.historyLoading).toBe(false);
  viki.client.close();
});

test("snapshot folds live cards and running state separately from finished history", () => {
  const { viki, socket } = setup();
  viki.actions.openSession("a");
  const data = snapshotData();
  data.events = [
    { type: "user.message", data: { text: "live", command: "", at: "" } },
    { type: "turn.started", data: { turn_id: NEXT } },
    { type: "permission.requested", data: permission, native_request: binding },
  ];
  data.state.mode = "plan";
  data.state.compacting = true;
  data.truncated = true;
  socket.synchronize("a", GENERATION, 20, data);
  const view = viki.store.getSnapshot().sessions.a;
  expect(view).toMatchObject({
    generation: GENERATION,
    seq: 20,
    synced: true,
    truncated: true,
    mode: "plan",
    compacting: true,
  });
  expect(view?.turns[0]).toMatchObject({ history: false, status: "running" });
  expect(view?.cards[0]?.nativeRequest).toEqual(binding);
  viki.client.close();
});

test("history errors stop automatic paging and leave session controls usable", async () => {
  vi.useFakeTimers();
  const { viki, socket } = setup();
  viki.actions.openSession("a");
  socket.synchronize("a");
  const history = last(socket, "history.more");
  socket.receive({
    v: 2,
    type: "history.page",
    session_id: "a",
    generation: GENERATION,
    request_id: history.request_id,
    data: { events: [], next_before_uuid: null, error: "too_large" },
  });
  expect(viki.store.getSnapshot().sessions.a).toMatchObject({
    historyError: "too_large",
    historyLoading: false,
    historyCursor: null,
  });
  viki.actions.loadOlder("a");
  vi.advanceTimersByTime(50_000);
  expect(socket.sent.filter((item) => item.type === "history.more")).toHaveLength(1);
  const change = viki.actions.setEffort("a", "low");
  socket.acknowledge(last(socket, "session.set_effort"));
  expect(await change).toBe("applied");
  expect(viki.store.getSnapshot().status).toBe("open");
  viki.client.close();
});

test("authoritative snapshot liveness updates the native session cap and clears a crashed runtime", () => {
  const { viki, socket } = setup();
  socket.receive({
    v: 2,
    type: "vault.sessions",
    session_id: "",
    generation: GENERATION,
    request_id: "",
    seq: 1,
    data: { others: [], live: [] },
  });
  const id = viki.actions.newSession();
  const data = snapshotData();
  data.state = {
    ...data.state,
    live: true,
    info: {
      model: "sonnet",
      mode: "auto",
      effort: "low",
      commands: [],
      agents: [],
      models: [],
      cli_version: "synthetic",
      resumed: false,
    },
  };
  socket.synchronize(id, GENERATION, 1, data);
  expect([...liveSessionIds(viki.store.getSnapshot(), new Set())]).toEqual([id]);
  socket.synchronize(id, GENERATION, 2, { ...data, state: { ...data.state, live: false } });
  expect([...liveSessionIds(viki.store.getSnapshot(), new Set())]).toEqual([]);
  viki.client.close();
});

// A test stood here that answered a card over the socket with a stale binding and expected the
// store to refuse it. The two actions it used were deleted on 2026-09-18, along with an
// amendment of `viki.accept.test.ts`, because every answer goes to the durable ledger.
// The invariant it guarded did not move: `state/approvals.unit.test.ts` covers a card with no
// native binding never being answered, a recorded request ignoring a later decide, and the
// opposite choice never replacing a recorded one, all on the path the app actually uses.
// What remains here is the half that belongs to this file, the replacement request itself.
test("a replacement request with the same tool-use ID replaces the card, and one closure clears it", () => {
  const { viki, socket } = setup();
  viki.actions.openSession("a");
  socket.synchronize("a");
  socket.receive(envelope(socket, "a", GENERATION, 1, "permission.requested", permission, binding));
  const replacement = { ...binding, runtime_id: NEXT, request_id: REPLACED };
  socket.receive(
    envelope(socket, "a", GENERATION, 2, "permission.requested", permission, replacement),
  );
  expect(viki.store.getSnapshot().sessions.a?.cards).toHaveLength(1);
  expect(viki.store.getSnapshot().sessions.a?.cards[0]?.nativeRequest).toEqual(replacement);
  // A closure that names the request this card replaced must not clear it: the tool-use ID is
  // the same, the request is not.
  socket.receive(
    envelope(socket, "a", GENERATION, 3, "request.answered", { tool_use_id: "tool" }, binding),
  );
  expect(viki.store.getSnapshot().sessions.a?.cards).toHaveLength(1);
  socket.receive(
    envelope(socket, "a", GENERATION, 4, "request.answered", { tool_use_id: "tool" }, replacement),
  );
  expect(viki.store.getSnapshot().sessions.a?.cards).toHaveLength(0);
  viki.client.close();
});

test("fork creates a target before subscribing and sends only after target synchronization", async () => {
  const { viki, socket } = setup();
  viki.actions.openSession("a");
  socket.synchronize("a");
  const fork = viki.actions.fork("a", "fork text");
  const creation = last(socket, "session.fork");
  expect(socket.sent.some((item) => item.type === "turn.send")).toBe(false);
  socket.acknowledge(creation);
  await flush();
  const id = String(creation.fork_id);
  expect(last(socket, "session.open").session_id).toBe(id);
  expect(socket.sent.some((item) => item.type === "turn.send")).toBe(false);
  socket.synchronize(id);
  await flush();
  const command = last(socket, "turn.send");
  expect(command).toMatchObject({ session_id: id, text: "fork text" });
  socket.acknowledge(command);
  expect(await fork).toBe(id);
  viki.client.close();
});

test("receipt timeout is unknown and does not retry or remove a pending card", async () => {
  vi.useFakeTimers();
  const { viki, socket } = setup();
  viki.actions.openSession("a");
  socket.synchronize("a");
  socket.receive(envelope(socket, "a", GENERATION, 1, "permission.requested", permission, binding));
  const response = viki.actions.setEffort("a", "low");
  vi.advanceTimersByTime(50);
  expect(await response).toBe("unknown");
  expect(viki.store.getSnapshot().sessions.a?.cards).toHaveLength(1);
  vi.advanceTimersByTime(50_000);
  expect(socket.sent.filter((item) => item.type === "session.set_effort")).toHaveLength(1);
  viki.client.close();
});

test("pending controls are bounded and disconnect settles every outstanding receipt", async () => {
  const { viki, socket } = setup();
  viki.actions.openSession("a");
  socket.synchronize("a");
  const pending = Array.from({ length: 64 }, () => viki.actions.setEffort("a", "low"));
  expect(await viki.actions.setEffort("a", "low")).toBe("rejected");
  expect(socket.sent.filter((item) => item.type === "session.set_effort")).toHaveLength(64);
  viki.client.close();
  expect(await Promise.all(pending)).toEqual(Array.from({ length: 64 }, () => "unknown"));
});

test("unsupported frames stop reconnect and keep the existing view", () => {
  vi.useFakeTimers();
  const { viki, socket, sockets } = setup();
  const id = viki.actions.newSession();
  socket.synchronize(id);
  socket.receive({ v: 1, type: "status", session_id: id, seq: 1, data: {} });
  expect(viki.store.getSnapshot().status).toBe("incompatible");
  expect(viki.store.getSnapshot().sessions[id]).toBeDefined();
  vi.advanceTimersByTime(50_000);
  expect(sockets).toHaveLength(1);
});
