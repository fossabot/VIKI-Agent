import { afterEach, expect, test, vi } from "vitest";
import { FakeSocket, GENERATION } from "./accept/m3b/fakes";
import { createSessionWindowsApi } from "./clients/conversations/sessionWindowsApi";
import { readSessionHandoff } from "./sessionHandoff";
import { createViki, readToken } from "./state/viki";

const id = "a0000000-0000-4000-8000-000000000001";
afterEach(() => {
  vi.useRealTimers();
  window.history.replaceState(null, "", "/");
  window.sessionStorage.clear();
});

test("target is consumed before launch token cleanup", () => {
  window.history.replaceState(null, "", `/#token=synthetic&session=${id}`);
  expect(readSessionHandoff()).toEqual({ kind: "target", sessionId: id });
  expect(readToken(window.location, window.sessionStorage, window.history)).toBe("synthetic");
  expect(window.location.hash).toBe("");
});
test.each(["", "#token=synthetic", "#other=1"])("no target in %s", (hash) =>
  expect(readSessionHandoff(hash)).toEqual({ kind: "none" }),
);
test.each(["", "unknown", id.toUpperCase(), `${id}&session=${id}`, "../private", "%00"])(
  "malformed target %s stays explicit",
  (target) => expect(readSessionHandoff(`#session=${target}`)).toEqual({ kind: "invalid" }),
);

test("window API authenticates exact target once and checks acknowledgement", async () => {
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    expect(init?.headers).toMatchObject({
      authorization: "Bearer synthetic",
      "content-type": "application/json",
    });
    expect(JSON.parse(String(init?.body))).toEqual({ session_id: id });
    return new Response('{"opened":true}', { status: 200 });
  });
  await createSessionWindowsApi("synthetic", fetchImpl).open(id);
  expect(fetchImpl).toHaveBeenCalledOnce();
});
test("invalid IDs never reach the endpoint; refusals do not retry", async () => {
  const fetchImpl = vi.fn(
    async () => new Response('{"error":{"code":"capacity"}}', { status: 429 }),
  );
  const api = createSessionWindowsApi("synthetic", fetchImpl);
  await expect(api.open("unknown")).rejects.toMatchObject({ code: "invalid_input" });
  expect(fetchImpl).not.toHaveBeenCalled();
  await expect(api.open(id)).rejects.toMatchObject({ code: "capacity" });
  expect(fetchImpl).toHaveBeenCalledOnce();
});
test.each(["{}", '{"opened":false}', '{"opened":true,"extra":1}'])(
  "bad success %s remains refused",
  async (body) => {
    await expect(
      createSessionWindowsApi(
        "synthetic",
        vi.fn(async () => new Response(body)),
      ).open(id),
    ).rejects.toMatchObject({ code: "unavailable" });
  },
);
test("caller cancellation stops intake and keeps failure explicit", async () => {
  const controller = new AbortController();
  const api = createSessionWindowsApi(
    "synthetic",
    vi.fn(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new Error("synthetic cancellation")),
          );
        }),
    ),
  );
  const work = api.open(id, controller.signal);
  controller.abort();
  await expect(work).rejects.toMatchObject({ code: "cancelled" });
});

function targeted() {
  const sockets: FakeSocket[] = [];
  const viki = createViki({
    url: "ws://synthetic",
    token: "synthetic",
    createSocket: () => {
      const socket = new FakeSocket(false);
      sockets.push(socket);
      return socket;
    },
    controlTimeoutMs: 20,
  });
  viki.client.connect();
  sockets[0]?.open();
  viki.actions.openExistingSession(id);
  return { viki, sockets, socket: sockets[0] as FakeSocket };
}
test.each(["unavailable", "storage_unavailable"])(
  "closing a view refused as %s releases it locally and keeps a later open existing-only",
  async (reason) => {
    const { viki, socket } = targeted();
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: id,
      generation: "",
      seq: 0,
      request_id: viki.client.subscriptionId(id),
      data: { reason },
    });
    socket.sent.length = 0;

    expect(await viki.actions.closeSession(id)).toBe("applied");
    expect(viki.store.getSnapshot().sessions[id]).toBeUndefined();
    expect(viki.client.subscriptionId(id)).toBeNull();
    expect(socket.sent).toEqual([]);

    viki.actions.openSession(id);
    expect(socket.sent).toMatchObject([
      { type: "session.open", session_id: id, existing_only: true },
    ]);
    expect(await viki.actions.closeSession(id)).toBe("rejected");
    expect(viki.store.getSnapshot().sessions[id]).toBeDefined();
    viki.client.close();
  },
);
test("a prior refusal cannot release a view whose reconnect has not synchronized", async () => {
  vi.useFakeTimers();
  const { viki, socket, sockets } = targeted();
  try {
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: id,
      generation: "",
      seq: 0,
      request_id: viki.client.subscriptionId(id),
      data: { reason: "unavailable" },
    });
    socket.onclose?.({ code: 1006 });
    vi.advanceTimersByTime(500);
    sockets[1]?.open();

    expect(viki.store.getSnapshot().sessions[id]?.openRefused).toBeNull();
    expect(await viki.actions.closeSession(id)).toBe("rejected");
    expect(viki.store.getSnapshot().sessions[id]).toBeDefined();
    expect(sockets[1]?.sent.filter((frame) => frame.type === "session.close")).toEqual([]);
  } finally {
    viki.client.close();
  }
});
test("an explicit refused-target retry gets a new request and rejects the prior refusal", () => {
  const { viki, socket } = targeted();
  const previous = viki.client.subscriptionId(id);
  const refuse = (requestId: string | null) =>
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: id,
      generation: "",
      seq: 0,
      request_id: requestId,
      data: { reason: "unavailable" },
    });
  refuse(previous);
  const before = socket.sent.length;
  viki.actions.openSession(id);
  expect(socket.sent).toHaveLength(before);
  viki.actions.openExistingSession(id);
  expect(viki.client.subscriptionId(id)).not.toBe(previous);
  expect(socket.sent.at(-1)).toMatchObject({ type: "session.open", existing_only: true });
  expect(viki.store.getSnapshot().sessions[id]?.openRefused).toBeNull();
  refuse(previous);
  expect(viki.store.getSnapshot().sessions[id]?.openRefused).toBeNull();
  socket.synchronize(id);
  expect(viki.store.getSnapshot().sessions[id]?.synced).toBe(true);
  viki.client.close();
});
test("existing-only subscribe remains strict after refusal and reconnect", () => {
  vi.useFakeTimers();
  const { viki, socket, sockets } = targeted();
  expect(socket.sent.at(-1)).toMatchObject({
    type: "session.open",
    session_id: id,
    existing_only: true,
  });
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
  expect(socket.sent.filter((msg) => msg.type === "history.more")).toEqual([]);
  viki.actions.openSession(id);
  socket.onclose?.({ code: 1006 });
  vi.advanceTimersByTime(500);
  sockets[1]?.open();
  expect(sockets[1]?.sent.at(-1)).toMatchObject({ type: "session.open", existing_only: true });
  sockets[1]?.synchronize(id);
  expect(viki.store.getSnapshot().sessions[id]?.synced).toBe(true);
  expect(viki.store.getSnapshot().sessions[id]?.openRefused).toBeUndefined();
  viki.client.close();
});
test.each(["stale", "wrong-generation", "bad-reason"])(
  "ignores %s refusal before generation admission",
  (kind) => {
    const { viki, socket } = targeted();
    socket.receive({
      v: 2,
      type: "session.open.refused",
      session_id: id,
      generation: kind === "wrong-generation" ? GENERATION : "",
      seq: 0,
      request_id: kind === "stale" ? "stale" : viki.client.subscriptionId(id),
      data: { reason: kind === "bad-reason" ? "invented" : "unavailable" },
    });
    expect(viki.store.getSnapshot().sessions[id]?.openRefused).toBeUndefined();
    viki.client.close();
  },
);
test("refusal immediately settles a pending target readiness without a mutation", async () => {
  const { viki, socket } = targeted();
  const result = viki.actions.prepareTarget(id);
  socket.receive({
    v: 2,
    type: "session.open.refused",
    session_id: id,
    generation: "",
    seq: 0,
    request_id: viki.client.subscriptionId(id),
    data: { reason: "storage_unavailable" },
  });
  expect(await result).toBeNull();
  expect(socket.sent.map((msg) => msg.type)).toEqual(["auth", "session.open"]);
  viki.client.close();
});
test("global re-discovery after close retains existing-only target policy", () => {
  const { viki, socket } = targeted();
  socket.synchronize(id);
  socket.receive({
    v: 2,
    type: "session.closed",
    session_id: id,
    generation: GENERATION,
    seq: 1,
    request_id: viki.client.subscriptionId(id),
    data: { reason: "requested" },
  });
  const global = (live: string[], seq: number) =>
    socket.receive({
      v: 2,
      type: "vault.sessions",
      session_id: "",
      generation: GENERATION,
      seq,
      request_id: "",
      data: { live, others: [] },
    });
  global([], 1);
  socket.sent.length = 0;
  global([id], 2);
  expect(socket.sent.at(-1)).toMatchObject({
    type: "session.open",
    session_id: id,
    existing_only: true,
  });
  viki.client.close();
});
test("existing-only upgrade re-correlates an already tracked ordinary session", () => {
  const socket = new FakeSocket(false);
  const viki = createViki({
    url: "ws://synthetic",
    token: "synthetic",
    createSocket: () => socket,
  });
  viki.client.connect();
  socket.open();
  viki.actions.openSession(id);
  const previous = viki.client.subscriptionId(id);
  viki.actions.openExistingSession(id);
  expect(viki.client.subscriptionId(id)).not.toBe(previous);
  expect(socket.sent.at(-1)).toMatchObject({ type: "session.open", existing_only: true });
  expect(viki.store.getSnapshot().sessions[id]?.requestId).toBe(viki.client.subscriptionId(id));
  viki.client.close();
});
