/** Acceptance (frozen): the WebSocket client's auth, resubscribe, queue and reconnect rules. */
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ClientMessage } from "../protocol";
import { SocketClient, type SocketStatus, type WebSocketLike } from "./socket";

class FakeSocket implements WebSocketLike {
  sent: unknown[] = [];
  closedWith: number | undefined;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  constructor(readonly url: string) {}

  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }

  close(code = 1000): void {
    this.closedWith = code;
  }

  open(): void {
    this.onopen?.({});
  }

  receive(message: unknown): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  drop(code = 1006): void {
    this.onclose?.({ code });
  }
}

function setup() {
  const sockets: FakeSocket[] = [];
  const statuses: SocketStatus[] = [];
  const client = new SocketClient({
    url: "ws://127.0.0.1:8765/ws",
    token: "tok",
    createSocket: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    retryDelaysMs: [100, 200],
  });
  client.onStatus((s) => statuses.push(s));
  return { client, sockets, statuses };
}

const send = (text: string): ClientMessage => ({
  type: "turn.send",
  session_id: "a",
  generation: "g",
  command_id: "c",
  text,
});

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

test("auth first, then correlated subscriptions; disconnected mutations are refused", () => {
  const { client, sockets, statuses } = setup();
  let seqA = 3;
  client.track("a", () => ({ generation: "g", seq: seqA }));
  client.track("b", () => ({ generation: "g", seq: 0 }));
  expect(client.send(send("queued while connecting"))).toBe(false);
  client.connect();
  expect(sockets).toHaveLength(1);
  expect(sockets[0]?.url).toBe("ws://127.0.0.1:8765/ws");
  seqA = 5; // the seq is read when the socket opens, not when tracked
  sockets[0]?.open();
  expect(sockets[0]?.sent).toEqual([
    { type: "auth", token: "tok", v: 2 },
    {
      type: "session.open",
      session_id: "a",
      after_seq: 5,
      generation: "g",
      request_id: expect.any(String),
    },
    {
      type: "session.open",
      session_id: "b",
      after_seq: 0,
      generation: "g",
      request_id: expect.any(String),
    },
  ]);
  expect(statuses).toEqual(["connecting", "open"]);
});

test("tracking while open subscribes at once, and only once", () => {
  const { client, sockets } = setup();
  client.connect();
  sockets[0]?.open();
  client.track("c", () => ({ generation: "g", seq: 7 }));
  client.track("c", () => ({ generation: "g", seq: 7 }));
  client.send(send("live"));
  expect(sockets[0]?.sent.slice(1)).toEqual([
    {
      type: "session.open",
      session_id: "c",
      after_seq: 7,
      generation: "g",
      request_id: expect.any(String),
    },
    send("live"),
  ]);
});

test("a dropped socket reconnects with backoff and resubscribes at the latest seq", () => {
  const { client, sockets, statuses } = setup();
  let seq = 1;
  client.track("a", () => ({ generation: "g", seq: seq }));
  client.connect();
  sockets[0]?.open();
  seq = 9;
  sockets[0]?.drop();
  expect(statuses.at(-1)).toBe("reconnecting");
  expect(client.send(send("while away"))).toBe(false);
  vi.advanceTimersByTime(99);
  expect(sockets).toHaveLength(1);
  vi.advanceTimersByTime(1);
  expect(sockets).toHaveLength(2);
  sockets[1]?.drop(); // failed again before opening: the next delay is 200
  vi.advanceTimersByTime(199);
  expect(sockets).toHaveLength(2);
  vi.advanceTimersByTime(1);
  sockets[2]?.drop(); // the last delay repeats
  vi.advanceTimersByTime(200);
  expect(sockets).toHaveLength(4);
  sockets[3]?.open();
  expect(sockets[3]?.sent).toEqual([
    { type: "auth", token: "tok", v: 2 },
    {
      type: "session.open",
      session_id: "a",
      after_seq: 9,
      generation: "g",
      request_id: expect.any(String),
    },
  ]);
  expect(statuses.at(-1)).toBe("open");
  sockets[3]?.drop(); // a successful open reset the backoff
  vi.advanceTimersByTime(100);
  expect(sockets).toHaveLength(5);
});

test("a refused token stops reconnecting", () => {
  const { client, sockets, statuses } = setup();
  client.connect();
  sockets[0]?.open();
  sockets[0]?.drop(4401);
  vi.advanceTimersByTime(10_000);
  expect(sockets).toHaveLength(1);
  expect(statuses.at(-1)).toBe("unauthorized");
});

test("close() ends the connection for good", () => {
  const { client, sockets, statuses } = setup();
  client.connect();
  sockets[0]?.open();
  client.close();
  expect(sockets[0]?.closedWith).toBeDefined();
  sockets[0]?.drop(1000);
  vi.advanceTimersByTime(10_000);
  expect(sockets).toHaveLength(1);
  expect(statuses.at(-1)).toBe("closed");
});

test("messages reach every handler until it unsubscribes; untracked sessions stay quiet", () => {
  const { client, sockets } = setup();
  const got: unknown[] = [];
  const stop = client.onMessage((m) => got.push(m));
  client.track("a", () => ({ generation: "g", seq: 0 }));
  client.untrack("a");
  client.connect();
  sockets[0]?.open();
  expect(sockets[0]?.sent).toEqual([{ type: "auth", token: "tok", v: 2 }]);
  sockets[0]?.receive({
    type: "status",
    session_id: "a",
    seq: 1,
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    data: { state: "", mode: "auto" },
  });
  stop();
  sockets[0]?.receive({
    type: "status",
    session_id: "a",
    seq: 2,
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    data: { state: "", mode: "plan" },
  });
  expect(got).toEqual([
    {
      type: "status",
      session_id: "a",
      seq: 1,
      v: 2,
      generation: "11111111-1111-4111-8111-111111111111",
      request_id: "",
      data: { state: "", mode: "auto" },
    },
  ]);
});
