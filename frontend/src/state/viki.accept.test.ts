/** Amendment: exact v2 subscriptions, control identity and token contracts. */
import { afterEach, expect, test, vi } from "vitest";
import { FakeSocket, GENERATION } from "../accept/m3b/fakes";
import { createViki, readToken } from "./viki";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const AT = "2026-09-11T08:00:00.000Z";
const BINDING = {
  runtime_id: "33333333-3333-4333-8333-333333333333",
  request_id: "44444444-4444-4444-8444-444444444444",
  turn_id: "55555555-5555-4555-8555-555555555555",
};
const openFrame = (id: string, generation?: string) => ({
  type: "session.open",
  session_id: id,
  after_seq: 0,
  request_id: expect.stringMatching(UUID),
  ...(generation ? { generation } : {}),
});
const historyFrame = (id: string, before?: string) => ({
  type: "history.more",
  session_id: id,
  generation: GENERATION,
  request_id: expect.stringMatching(UUID),
  ...(before ? { before_uuid: before } : {}),
});
const commandFrame = (type: string, id: string, extra: Record<string, unknown> = {}) => ({
  type,
  session_id: id,
  generation: GENERATION,
  command_id: expect.stringMatching(UUID),
  ...extra,
});
function connected() {
  const socket = new FakeSocket(false);
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "tok",
    createSocket: () => socket,
  });
  viki.client.connect();
  socket.open();
  socket.sent.length = 0;
  const sync = (id: string) => socket.synchronize(id, GENERATION, 0);
  const receive = (
    type: string,
    id: string,
    seq: number,
    data: unknown,
    native_request?: typeof BINDING,
  ) =>
    socket.receive({
      v: 2,
      type,
      session_id: id,
      generation: GENERATION,
      request_id: viki.client.subscriptionId(id) ?? "",
      seq,
      data,
      ...(native_request ? { native_request } : {}),
    });
  const page = (id: string, next: string | null) => {
    const request = socket.sent.findLast((m) => m.type === "history.more" && m.session_id === id);
    socket.receive({
      v: 2,
      type: "history.page",
      session_id: id,
      generation: GENERATION,
      request_id: request?.request_id,
      data: { events: [], next_before_uuid: next },
    });
  };
  return { viki, socket, sync, receive, page };
}
afterEach(() => vi.useRealTimers());

test("opening a session subscribes and asks for its first history page once", () => {
  const { viki, socket, sync, page } = connected();
  viki.actions.openSession("a");
  viki.actions.openSession("a");
  viki.actions.loadOlder("a");
  expect(socket.sent).toEqual([openFrame("a")]);
  expect(viki.store.getSnapshot().sessions.a?.historyLoading).toBe(true);
  sync("a");
  expect(socket.sent).toEqual([openFrame("a"), historyFrame("a")]);
  page("a", "u-9");
  socket.sent.length = 0;
  viki.actions.loadOlder("a");
  viki.actions.loadOlder("a");
  expect(socket.sent).toEqual([historyFrame("a", "u-9")]);
  page("a", null);
  socket.sent.length = 0;
  viki.actions.loadOlder("a");
  expect(socket.sent).toEqual([]);
  expect(viki.store.getSnapshot().status).toBe("open");
  viki.client.close();
});

test("a history request lost with the socket is sent again after reconnect synchronization once", () => {
  vi.useFakeTimers();
  const { viki, socket, sync } = connected();
  viki.actions.openSession("a");
  sync("a");
  const old = socket.sent.find((m) => m.type === "history.more")?.request_id;
  socket.sent.length = 0;
  socket.onclose?.({ code: 1006 });
  viki.actions.openSession("b");
  expect(socket.sent).toEqual([]);
  vi.runOnlyPendingTimers();
  socket.open();
  expect(socket.sent).toEqual([
    { type: "auth", token: "tok", v: 2 },
    openFrame("a", GENERATION),
    openFrame("b"),
  ]);
  sync("a");
  sync("b");
  expect(socket.sent.slice(3)).toEqual([historyFrame("a"), historyFrame("b")]);
  expect(socket.sent[3]?.request_id).not.toBe(old);
  viki.client.close();
});

test("new sessions and forks get fresh IDs and fork text waits for target synchronization", async () => {
  const { viki, socket, sync } = connected();
  const id = viki.actions.newSession();
  expect(id).toMatch(UUID);
  expect(socket.sent).toEqual([openFrame(id)]);
  expect(viki.store.getSnapshot().sessions[id]?.historyCursor).toBeNull();
  sync(id);
  socket.sent.length = 0;
  viki.actions.loadOlder(id);
  expect(socket.sent).toEqual([]);
  const pending = viki.actions.fork(id, "branch prompt");
  await Promise.resolve();
  const fork = socket.sent.find((m) => m.type === "session.fork");
  expect(fork?.fork_id).toMatch(UUID);
  expect(fork?.fork_id).not.toBe(id);
  expect(socket.sent).toEqual([commandFrame("session.fork", id, { fork_id: fork?.fork_id })]);
  if (!fork) throw new Error("Missing fork command");
  socket.acknowledge(fork);
  await Promise.resolve();
  await Promise.resolve();
  const target = String(fork?.fork_id);
  expect(socket.sent.some((m) => m.type === "turn.send")).toBe(false);
  expect(socket.sent.at(-1)).toEqual(openFrame(target));
  sync(target);
  await Promise.resolve();
  await Promise.resolve();
  const turn = socket.sent.find((m) => m.type === "turn.send");
  expect(turn).toEqual(commandFrame("turn.send", target, { text: "branch prompt" }));
  if (!turn) throw new Error("Missing turn command");
  socket.acknowledge(turn);
  expect(await pending).toBe(target);
  viki.client.close();
});

test("closing waits for the authoritative receipt and reopening starts from the transcript", async () => {
  const { viki, socket, sync, receive } = connected();
  viki.actions.openSession("a");
  sync("a");
  receive("user.message", "a", 1, { text: "hi", command: "", at: AT });
  socket.sent.length = 0;
  const closing = viki.actions.closeSession("a");
  expect(socket.sent).toEqual([commandFrame("session.close", "a")]);
  expect(viki.store.getSnapshot().sessions.a).toBeDefined();
  const closeCommand = socket.sent[0];
  if (!closeCommand) throw new Error("Missing close command");
  socket.acknowledge(closeCommand);
  await closing;
  expect(viki.store.getSnapshot().sessions.a).toBeUndefined();
  receive("user.message", "a", 2, { text: "late", command: "", at: AT });
  expect(viki.store.getSnapshot().sessions.a).toBeUndefined();
  socket.sent.length = 0;
  viki.actions.openSession("a");
  expect(socket.sent).toEqual([openFrame("a")]);
  sync("a");
  expect(socket.sent.at(-1)).toEqual(historyFrame("a"));
  viki.client.close();
});

test("sessions this page never opened stay out of the store", () => {
  const { viki, socket, receive } = connected();
  receive("user.message", "b", 250, { text: "other", command: "", at: AT });
  expect(viki.store.getSnapshot().sessions).toEqual({});
  viki.actions.openSession("b");
  expect(socket.sent).toEqual([openFrame("b")]);
  viki.client.close();
});

test("a session closed elsewhere is untracked and its view removed", () => {
  const { viki, socket, sync, receive } = connected();
  viki.actions.openSession("a");
  sync("a");
  socket.sent.length = 0;
  receive("session.closed", "a", 1, { reason: "requested" });
  expect(viki.store.getSnapshot().sessions.a).toBeUndefined();
  receive("user.message", "a", 2, { text: "late", command: "", at: AT });
  expect(viki.store.getSnapshot().sessions.a).toBeUndefined();
  expect(socket.sent).toEqual([]);
  viki.actions.openSession("a");
  expect(socket.sent).toEqual([openFrame("a")]);
  viki.client.close();
});

test("the sessions the app still holds live are opened when the server lists them", () => {
  const { viki, socket, sync } = connected();
  const other = { session_id: "x", entrypoint: "claude-vscode", pid: 42, title: "" };
  viki.actions.openSession("a");
  sync("a");
  socket.sent.length = 0;
  socket.receive({
    v: 2,
    generation: GENERATION,
    request_id: "",
    type: "vault.sessions",
    session_id: "",
    seq: 1,
    data: { others: [other], live: ["a", "b"] },
  });
  expect(socket.sent).toEqual([openFrame("b")]);
  sync("b");
  expect(socket.sent.at(-1)).toEqual(historyFrame("b"));
  expect(viki.store.getSnapshot().others).toEqual([other]);
  socket.sent.length = 0;
  socket.receive({
    v: 2,
    generation: GENERATION,
    request_id: "",
    type: "vault.sessions",
    session_id: "",
    seq: 2,
    data: { others: [], live: ["b"] },
  });
  expect(socket.sent).toEqual([]);
  expect(viki.store.getSnapshot().others).toEqual([]);
  viki.client.close();
});

// Amended 2026-09-18. This test used to answer both cards through
// `actions.respondPermission` and `actions.answerQuestion` and assert the two socket frames they
// produced. Every card answer is recorded in the durable ledger over REST before it
// is delivered, so `state/approvals.ts` owns that path and those two actions had no caller but
// this file. The assertion here was the only thing keeping them alive, and deleting them was
// blocked until this line changed. What the test pins now is the half that never depended on the
// transport, and it pins it harder: a card leaves the store only when the server closes its
// request, one card at a time, and nothing the client does removes one.
test("cards leave the store only when the server closes their request", () => {
  const { viki, socket, sync, receive } = connected();
  viki.actions.openSession("a");
  sync("a");
  const question = { ...BINDING, request_id: "66666666-6666-4666-8666-666666666666" };
  receive(
    "permission.requested",
    "a",
    1,
    {
      tool_use_id: "p1",
      kind: "tool",
      tool: "Bash",
      summary: "x",
      question: "",
      reason: "",
      agent: "",
      plan: "",
    },
    BINDING,
  );
  receive("question.asked", "a", 2, { tool_use_id: "q1", questions: [] }, question);
  expect(viki.store.getSnapshot().sessions.a?.cards).toHaveLength(2);
  socket.sent.length = 0;
  receive("request.answered", "a", 3, { tool_use_id: "p1" }, BINDING);
  expect(viki.store.getSnapshot().sessions.a?.cards).toHaveLength(1);
  receive("request.cancelled", "a", 4, { tool_use_id: "q1" }, question);
  expect(viki.store.getSnapshot().sessions.a?.cards).toEqual([]);
  expect(socket.sent).toEqual([]);
  viki.client.close();
});

test("the other actions map one-to-one onto guarded client messages", async () => {
  const { viki, socket, sync } = connected();
  viki.actions.openSession("a");
  sync("a");
  socket.sent.length = 0;
  const send = viki.actions.send("a", "hi");
  await Promise.resolve();
  const tasks = [
    viki.actions.interrupt("a"),
    viki.actions.setModel("a", "opus"),
    viki.actions.setEffort("a", "max"),
  ];
  viki.actions.openUrl("https://example.com");
  viki.actions.openVault("30-knowledge/x.md");
  tasks.push(viki.actions.closeSession("a"));
  expect(socket.sent).toEqual([
    commandFrame("turn.send", "a", { text: "hi" }),
    commandFrame("turn.interrupt", "a"),
    commandFrame("session.set_model", "a", { model: "opus" }),
    commandFrame("session.set_effort", "a", { effort: "max" }),
    { type: "open.url", url: "https://example.com" },
    { type: "open.vault", path: "30-knowledge/x.md" },
    commandFrame("session.close", "a"),
  ]);
  for (const message of socket.sent) if ("command_id" in message) socket.acknowledge(message);
  await Promise.all([send, ...tasks]);
  viki.client.close();
});

test("the token comes from the fragment once, then from session storage", () => {
  const store = new Map<string, string>();
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
  };
  const history = { replaceState: vi.fn() };
  expect(readToken({ hash: "#token=abc", pathname: "/", search: "" }, storage, history)).toBe(
    "abc",
  );
  expect(store.get("viki.token")).toBe("abc");
  expect(history.replaceState).toHaveBeenCalledWith(null, "", "/");
  expect(readToken({ hash: "", pathname: "/", search: "" }, storage, history)).toBe("abc");
  expect(
    readToken(
      { hash: "", pathname: "/", search: "" },
      { getItem: () => null, setItem: () => {} },
      history,
    ),
  ).toBeNull();
});
