import { afterEach, expect, test, vi } from "vitest";
import { createDictationApi, DictationApiError } from "./dictationApi";
import type { DictationWebSocket } from "./dictationSocket";
import type { VoiceServerMessage, VoiceSocketHandlers } from "./dictationTypes";

const ID = "11111111-1111-4111-8111-111111111111";
const STATUS = {
  state: "ready",
  running: false,
  model: "small-q8_0",
  backend: "cpu",
  device: null,
  languages: ["auto", "en", "ja"],
  language: "auto",
  language_unavailable: false,
  models: ["small-q8_0"],
  backends: ["cpu"],
  fallback: { backend: "cuda", code: "gpu_failed", detail: null },
  cpu_model: "small-q8_0",
  slow_on_cpu: false,
};

class FakeSocket implements DictationWebSocket {
  binaryType = "blob";
  sent: (string | ArrayBuffer)[] = [];
  closedWith: number[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  send(data: string | ArrayBuffer) {
    this.sent.push(data);
  }
  close(code?: number) {
    this.closedWith.push(code ?? 1005);
  }
  say(data: unknown) {
    this.onmessage?.({ data: typeof data === "string" ? data : JSON.stringify(data) });
  }
}

const deps = { workletUrl: "/w.js", startCapture: vi.fn(), microphones: vi.fn() };
function open(location = { protocol: "http:", host: "127.0.0.1:8765" }) {
  const fake = new FakeSocket();
  const urls: string[] = [];
  const messages: VoiceServerMessage[] = [];
  const closed: number[] = [];
  const handlers: VoiceSocketHandlers = {
    message: (message) => messages.push(message),
    closed: (code) => closed.push(code),
  };
  const api = createDictationApi("tok", {
    ...deps,
    location,
    createSocket: (url) => {
      urls.push(url);
      return fake;
    },
  });
  return { socket: api.socket(handlers), fake, urls, messages, closed };
}

function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
const code = (promise: Promise<unknown>) =>
  promise.then(
    () => null,
    (error: unknown) => (error instanceof DictationApiError ? error.code : "other"),
  );
const status = () => createDictationApi("tok", deps).status();

afterEach(() => vi.unstubAllGlobals());

test("status parses the contract's body with the Bearer header", async () => {
  const fetchMock = stubFetch({ voice: STATUS });
  expect(await status()).toEqual(STATUS);
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/v2/voice");
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual({ authorization: "Bearer tok" });
});

test("status accepts a null fallback and model", async () => {
  stubFetch({ voice: { ...STATUS, fallback: null, model: null, cpu_model: null } });
  expect((await status()).fallback).toBeNull();
});

test("an extra key, a wrong type or a missing key is unavailable", async () => {
  const { slow_on_cpu: _drop, ...missing } = STATUS;
  for (const body of [
    { voice: { ...STATUS, extra: 1 } },
    { voice: STATUS, extra: 1 },
    { voice: { ...STATUS, running: "no" } },
    { voice: { ...STATUS, state: "weird" } },
    { voice: { ...STATUS, languages: [1] } },
    { voice: { ...STATUS, fallback: { backend: "cuda", code: "x" } } },
    { voice: missing },
  ]) {
    stubFetch(body);
    expect(await code(status())).toBe("unavailable");
  }
});

test("a refusal's code is the error code", async () => {
  stubFetch({ error: { code: "unauthorized", message: "no" } }, 401);
  expect(await code(status())).toBe("unauthorized");
  stubFetch("nonsense", 500);
  expect(await code(status())).toBe("unavailable");
});

test("clearFallback posts an empty object", async () => {
  const fetchMock = stubFetch({ voice: STATUS });
  await createDictationApi("tok", deps).clearFallback();
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/v2/voice/fallback/clear");
  expect(init.method).toBe("POST");
  expect(init.body).toBe("{}");
  expect(init.headers).toEqual({
    authorization: "Bearer tok",
    "content-type": "application/json",
  });
});

test("report posts one event and code and swallows a failure", async () => {
  const fetchMock = vi.fn(async () => new Response("ok", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const api = createDictationApi("tok", deps);
  api.report?.("dropped", "mic");
  expect(fetchMock).toHaveBeenCalledOnce();
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/v2/voice/handsfree/report");
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({
    authorization: "Bearer tok",
    "content-type": "application/json",
  });
  expect(init.body).toBe(JSON.stringify({ event: "dropped", code: "mic" }));
  const fetchFailMock = vi.fn(async () => Promise.reject(new Error("network")));
  vi.stubGlobal("fetch", fetchFailMock);
  const api2 = createDictationApi("tok", deps);
  api2.report?.("failed", "audio_failed");
  expect(fetchFailMock).toHaveBeenCalledOnce();
});

test("the socket url follows the page's scheme and host", () => {
  expect(open().urls).toEqual(["ws://127.0.0.1:8765/ws/voice"]);
  expect(open({ protocol: "https:", host: "localhost:8765" }).urls).toEqual([
    "wss://localhost:8765/ws/voice",
  ]);
});

test("sends before open queue; auth goes first, then begin, audio and end", () => {
  const { socket, fake } = open();
  expect(fake.binaryType).toBe("arraybuffer");
  const audio = new ArrayBuffer(4);
  socket.begin(ID);
  socket.audio(audio);
  expect(fake.sent).toEqual([]);
  fake.onopen?.({});
  socket.end(ID);
  socket.cancel(ID);
  expect(fake.sent.map((data) => (typeof data === "string" ? JSON.parse(data) : data))).toEqual([
    { type: "auth", token: "tok", v: 2 },
    { type: "begin", capture_id: ID },
    audio,
    { type: "end", capture_id: ID },
    { type: "cancel", capture_id: ID },
  ]);
});

test("each server message type parses", () => {
  const { fake, messages, closed } = open();
  fake.onopen?.({});
  const all: VoiceServerMessage[] = [
    { type: "starting", capture_id: ID },
    {
      type: "capturing",
      capture_id: ID,
      model: "m",
      backend: "cpu",
      device: null,
      language: "auto",
      language_unavailable: false,
      fallback: { backend: "cuda", code: "c", detail: "d" },
    },
    { type: "partial", capture_id: ID, segment: 0, revision: 1, text: "hi" },
    { type: "final", capture_id: ID, segment: 0, text: "hi" },
    { type: "stopping", capture_id: ID, reason: "capture_cap" },
    { type: "done", capture_id: ID },
    { type: "cancelled", capture_id: ID },
    { type: "error", capture_id: null, code: "busy" },
  ];
  for (const message of all) fake.say(message);
  expect(messages).toEqual(all);
  expect(closed).toEqual([]);
});

test("a malformed server message closes the socket with 4400", () => {
  const bad: unknown[] = [
    "not json",
    "[]",
    { type: "nope", capture_id: ID },
    { type: "done" },
    { type: "partial", capture_id: ID, segment: -1, revision: 0, text: "" },
    { type: "stopping", capture_id: ID, reason: "other" },
    new ArrayBuffer(2),
  ];
  for (const message of bad) {
    const { fake, messages, closed } = open();
    fake.onopen?.({});
    if (message instanceof ArrayBuffer) fake.onmessage?.({ data: message });
    else fake.say(message);
    expect(fake.closedWith).toEqual([4400]);
    expect(closed).toEqual([4400]);
    expect(messages).toEqual([]);
  }
});

test("a server message with an extra key closes the socket", () => {
  const { fake, messages, closed } = open();
  fake.onopen?.({});
  fake.say({ type: "done", capture_id: ID, extra: 1 });
  expect(messages).toEqual([]);
  expect(closed).toEqual([4400]);
});

test("closed fires once, and 1006 when the socket never opened", () => {
  const never = open();
  never.fake.onclose?.({ code: 1000 });
  never.fake.onclose?.({ code: 1000 });
  expect(never.closed).toEqual([1006]);
  const used = open();
  used.fake.onopen?.({});
  used.fake.onclose?.({ code: 1013 });
  used.fake.onclose?.({ code: 1000 });
  expect(used.closed).toEqual([1013]);
  used.socket.close();
  expect(used.fake.closedWith).toEqual([]);
});

test("close is idempotent and a closed socket sends nothing", () => {
  const { socket, fake } = open();
  fake.onopen?.({});
  socket.close();
  socket.close();
  expect(fake.closedWith).toEqual([1000]);
  fake.onclose?.({ code: 1000 });
  socket.begin(ID);
  expect(fake.sent).toHaveLength(1);
});

// One api serves one window; it records each socket it creates, in order.
function oneWindow() {
  const fakes: FakeSocket[] = [];
  const api = createDictationApi("tok", {
    ...deps,
    location: { protocol: "http:", host: "127.0.0.1:8765" },
    createSocket: () => {
      const fake = new FakeSocket();
      fakes.push(fake);
      return fake;
    },
  });
  const socket = (closed: number[] = []) =>
    api.socket({ message: () => undefined, closed: (code) => closed.push(code) });
  return { fakes, socket };
}
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const commands = (fake: FakeSocket | undefined) =>
  fake?.sent.map((data) => JSON.parse(data as string));
const OTHER = "22222222-2222-4222-8222-222222222222";

test("a window's next socket connects once the open one closed, then sends auth first", async () => {
  const { fakes, socket } = oneWindow();
  socket();
  expect(fakes).toHaveLength(1);
  fakes[0]?.onopen?.({});
  socket().begin(ID);
  await flush();
  expect(fakes).toHaveLength(1);
  fakes[0]?.onclose?.({ code: 1000 });
  await flush();
  expect(fakes).toHaveLength(2);
  expect(fakes[1]?.binaryType).toBe("arraybuffer");
  fakes[1]?.onopen?.({});
  expect(commands(fakes[1])).toEqual([
    { type: "auth", token: "tok", v: 2 },
    { type: "begin", capture_id: ID },
  ]);
});

test("a socket closed while it waits ends 1000 at once, never connects and holds no turn", async () => {
  const { fakes, socket } = oneWindow();
  socket();
  const closed: number[] = [];
  const waiting = socket(closed);
  waiting.begin(ID);
  socket().begin(OTHER);
  waiting.close();
  waiting.close();
  expect(closed).toEqual([1000]);
  await flush();
  // The later socket still waits for the open one, which holds the server's slot.
  expect(fakes).toHaveLength(1);
  fakes[0]?.onclose?.({ code: 1000 });
  await flush();
  expect(fakes).toHaveLength(2);
  fakes[1]?.onopen?.({});
  expect(commands(fakes[1])).toEqual([
    { type: "auth", token: "tok", v: 2 },
    { type: "begin", capture_id: OTHER },
  ]);
  fakes[1]?.onclose?.({ code: 1000 });
  socket();
  expect(fakes).toHaveLength(3);
  expect(closed).toEqual([1000]);
});
