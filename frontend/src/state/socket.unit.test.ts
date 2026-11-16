/**
 * Unit tests pinning SocketClient rules that socket.accept.test.ts leaves implicit: the default
 * retry-delay table and its repeating tail, close() cancelling a pending retry, frames arriving
 * on a socket that has since been replaced, untrack() suppressing a resubscribe, and the frame
 * cap that keeps one oversize message from taking the whole connection down with it.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; Node itself is present
// under Vitest, which runs tests through Node, not a bundled browser.
import { readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ClientMessage } from "../protocol";
import {
  frameByteLength,
  MAX_FRAME_BYTES,
  SocketClient,
  type SocketStatus,
  turnSendFits,
  type WebSocketLike,
} from "./socket";

/** Relative to THIS file: frontend/src/state -> the repo root. */
const SERVER_PY = "../../../src/viki_agent/server.py";

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

function setup(retryDelaysMs?: readonly number[]) {
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
    ...(retryDelaysMs ? { retryDelaysMs } : {}),
  });
  client.onStatus((s) => statuses.push(s));
  return { client, sockets, statuses };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

test("with no retryDelaysMs option, the default table is [500, 1000, 2000, 5000] and the last delay repeats", () => {
  const { client, sockets } = setup();
  client.connect();
  sockets[0]?.open();
  for (const delay of [500, 1000, 2000, 5000, 5000]) {
    const before = sockets.length;
    sockets.at(-1)?.drop();
    vi.advanceTimersByTime(delay - 1);
    expect(sockets).toHaveLength(before);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(before + 1);
  }
});

test("an empty retryDelaysMs array falls back to the default table rather than never retrying", () => {
  const { client, sockets } = setup([]);
  client.connect();
  sockets[0]?.open();
  sockets[0]?.drop();
  vi.advanceTimersByTime(499);
  expect(sockets).toHaveLength(1);
  vi.advanceTimersByTime(1);
  expect(sockets).toHaveLength(2);
});

test("close() cancels a pending retry timer, so no further socket is created", () => {
  const { client, sockets, statuses } = setup([100, 200]);
  client.connect();
  sockets[0]?.open();
  sockets[0]?.drop(); // schedules a reconnect in 100ms
  client.close();
  vi.advanceTimersByTime(10_000);
  expect(sockets).toHaveLength(1);
  expect(statuses.at(-1)).toBe("closed");
});

test("a frame delivered late on a socket that has since been replaced never reaches handlers", () => {
  const { client, sockets } = setup([100, 200]);
  const got: unknown[] = [];
  client.onMessage((m) => got.push(m));
  client.connect();
  sockets[0]?.open();
  sockets[0]?.drop();
  vi.advanceTimersByTime(100); // dial() again; this.socket is now sockets[1]
  expect(sockets).toHaveLength(2);
  sockets[0]?.receive({
    type: "status",
    session_id: "a",
    seq: 1,
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    data: { state: "", mode: "x" },
  });
  expect(got).toEqual([]);
});

test("a second close event on an already-superseded socket schedules no extra reconnect", () => {
  const { client, sockets } = setup([100, 200]);
  client.connect();
  sockets[0]?.open();
  sockets[0]?.drop();
  vi.advanceTimersByTime(100);
  expect(sockets).toHaveLength(2);
  sockets[0]?.drop(); // stale: this.socket is sockets[1], not sockets[0]
  vi.advanceTimersByTime(10_000);
  expect(sockets).toHaveLength(2);
});

test("track() while open subscribes at once, and untrack() stops that id resubscribing on reconnect", () => {
  const { client, sockets } = setup([100, 200]);
  client.connect();
  sockets[0]?.open();
  client.track("a", () => ({ generation: null, seq: 3 }));
  expect(sockets[0]?.sent.at(-1)).toEqual({
    type: "session.open",
    session_id: "a",
    after_seq: 3,
    request_id: expect.any(String),
  });
  client.track("b", () => ({ generation: null, seq: 0 }));
  client.untrack("a");
  sockets[0]?.drop();
  vi.advanceTimersByTime(100);
  sockets[1]?.open();
  expect(sockets[1]?.sent).toEqual([
    { type: "auth", token: "tok", v: 2 },
    { type: "session.open", session_id: "b", after_seq: 0, request_id: expect.any(String) },
  ]);
});

test.each([null, "model-a"])(
  "a model expectation survives reconnect until its own barrier (%s)",
  (model) => {
    const { client, sockets } = setup([1]);
    client.connect();
    sockets[0]?.open();
    client.track("a", () => ({ generation: null, seq: 0 }), false, model);
    expect(sockets[0]?.sent.at(-1)).toMatchObject({ type: "session.open", expected_model: model });
    const oldRequest = client.subscriptionId("a") ?? "";
    sockets[0]?.drop();
    vi.advanceTimersByTime(1);
    sockets[1]?.open();
    expect(sockets[1]?.sent.at(-1)).toMatchObject({ expected_model: model });
    client.confirmSubscription("a", oldRequest); // an abandoned connection cannot clear it
    sockets[1]?.drop();
    vi.advanceTimersByTime(1);
    sockets[2]?.open();
    expect(sockets[2]?.sent.at(-1)).toMatchObject({ expected_model: model });
    client.confirmSubscription("a", client.subscriptionId("a") ?? "");
    sockets[2]?.drop();
    vi.advanceTimersByTime(1);
    sockets[3]?.open();
    expect(sockets[3]?.sent.at(-1)).not.toHaveProperty("expected_model");
    client.close();
  },
);

test("an auth-typed message passed to send() is dropped, never queued or delivered", () => {
  const { client, sockets } = setup([100, 200]);
  client.send({ type: "auth", token: "sneaky" } as unknown as ClientMessage);
  client.connect();
  sockets[0]?.open();
  expect(sockets[0]?.sent).toEqual([{ type: "auth", token: "tok", v: 2 }]);
});

test("connect() while a retry is pending dials no second socket", () => {
  const { client, sockets } = setup([100]);
  client.connect();
  sockets[0]?.open();
  sockets[0]?.drop();
  client.connect();
  expect(sockets).toHaveLength(1);
  vi.advanceTimersByTime(100);
  expect(sockets).toHaveLength(2);
});

test("close() reports closed once, and a later send() is refused", () => {
  const { client, sockets, statuses } = setup();
  client.connect();
  sockets[0]?.open();
  client.close();
  client.close();
  expect(statuses.filter((s) => s === "closed")).toHaveLength(1);
  expect(sockets[0]?.closedWith).toBe(1000);
  client.send({ type: "turn.interrupt", session_id: "a", generation: "gen", command_id: "cmd" });
  expect(sockets[0]?.sent).toEqual([{ type: "auth", token: "tok", v: 2 }]);
});

test("a frame over the cap is dropped rather than sent: the connection must survive it", () => {
  const { client, sockets } = setup([100, 200]);
  client.connect();
  sockets[0]?.open();
  // websockets fails the whole connection with close 1009 on an oversize message, so "the
  // server will reject it" is not an option that exists here: every live session's stream would
  // go down with this one frame.
  client.send({
    type: "turn.send",
    session_id: "a",
    generation: "gen",
    command_id: "cmd",
    text: "x".repeat(MAX_FRAME_BYTES),
  });
  // And the socket is still usable afterwards: the next frame goes out on the same one.
  client.send({ type: "turn.interrupt", session_id: "a", generation: "gen", command_id: "cmd" });
  expect(sockets[0]?.sent).toEqual([
    { type: "auth", token: "tok", v: 2 },
    { type: "turn.interrupt", session_id: "a", generation: "gen", command_id: "cmd" },
  ]);
});

test("an over-cap frame refused while disconnected is not queued either", () => {
  // Queuing it would be the same failure with a delay: the flush at the next onopen would send
  // it and kill the connection that had just come back.
  const { client, sockets } = setup([100, 200]);
  client.send({
    type: "turn.send",
    session_id: "a",
    generation: "gen",
    command_id: "cmd",
    text: "x".repeat(MAX_FRAME_BYTES),
  });
  client.connect();
  sockets[0]?.open();
  expect(sockets[0]?.sent).toEqual([{ type: "auth", token: "tok", v: 2 }]);
});

test("the budget is the serialised frame's UTF-8 bytes, not the text's own length", () => {
  // A newline is one JS character and one UTF-8 byte, but JSON.stringify writes it as the two
  // characters backslash-n. Measuring the raw text would let a frame through well over the cap.
  const newlines: ClientMessage = {
    type: "turn.send",
    session_id: "a",
    generation: "gen",
    command_id: "cmd",
    text: "\n".repeat(10),
  };
  expect(JSON.stringify(newlines)).toContain("\\n");
  expect(frameByteLength(newlines)).toBe(JSON.stringify(newlines).length);
  // A Japanese character is one JS character and three UTF-8 bytes; the server counts bytes.
  const jp: ClientMessage = {
    type: "turn.send",
    session_id: "a",
    generation: "gen",
    command_id: "cmd",
    text: "あ".repeat(10),
  };
  expect(frameByteLength(jp)).toBe(JSON.stringify(jp).length + 20);
});

test("turnSendFits charges the envelope, so what it admits SocketClient.send also sends", () => {
  const overhead = frameByteLength({
    type: "turn.send",
    session_id: "00000000-0000-0000-0000-000000000000",
    text: "",
    generation: "00000000-0000-0000-0000-000000000000",
    command_id: "00000000-0000-0000-0000-000000000000",
  });
  const admitted = "x".repeat(MAX_FRAME_BYTES - overhead);
  expect(turnSendFits(admitted)).toBe(true);
  expect(turnSendFits(`${admitted}x`)).toBe(false);
  // The frame actually sent is never larger than the one measured, so nothing App admits is
  // then dropped by the socket's own check underneath it — which would lose the text silently.
  expect(
    frameByteLength({
      type: "turn.send",
      session_id: "a",
      generation: "gen",
      command_id: "cmd",
      text: admitted,
    }),
  ).toBeLessThanOrEqual(MAX_FRAME_BYTES);
});

test("MAX_FRAME_BYTES still matches the server's WS_MAX_SIZE", () => {
  // The pair unload.ts's WRITE_TOOLS has with session.py, in the other direction: Python and TS
  // share no module, so this assertion is the only thing keeping the two numbers together.
  // Raising one alone would put the cliff back — a frame this side admits, uvicorn kills.
  // `here` must be a variable, not inlined: `new URL(literal, import.meta.url)` written
  // directly is Vite's own special asset-URL pattern, rewritten at build time into a
  // dev-server URL instead of a real path (App.test.tsx carries the same note).
  const here = import.meta.url;
  const source: string = readFileSync(fileURLToPath(new URL(SERVER_PY, here)), "utf-8");
  const match = /^WS_MAX_SIZE = (.+)$/m.exec(source);
  expect(match, "WS_MAX_SIZE not found in server.py").toBeTruthy();
  expect(match?.[1]?.split("#")[0]?.trim()).toBe("1 << 20");
  expect(MAX_FRAME_BYTES).toBe(1 << 20);
});
