import { expect, test, vi } from "vitest";
import { createPreviewsApi, PreviewsApiError } from "./previewsApi";

const W = "22222222-2222-4222-8222-222222222222";
const T = "33333333-3333-4333-8333-333333333333";
const P = "66666666-6666-4666-8666-666666666666";
const AUTH = { authorization: "Bearer tok" };
const JSON_HEADERS = { ...AUTH, "content-type": "application/json" };
const TIME = "2026-10-04T01:02:03.456Z";

const preview = (over: Record<string, unknown> = {}) => ({
  preview_id: P,
  workspace_id: W,
  worktree_id: T,
  command: "npm run dev",
  port: 5173,
  state: "listening",
  pid: 4242,
  listener_pid: 4300,
  exit_code: null,
  started_at: TIME,
  ended_at: null,
  ...over,
});
const exited = (over: Record<string, unknown> = {}) =>
  preview({
    state: "exited",
    listener_pid: null,
    exit_code: 1,
    ended_at: TIME,
    ...over,
  });

function reply(body: unknown, status = 200): typeof fetch {
  return vi.fn(async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
}
const call = (fetchImpl: typeof fetch) =>
  vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
const code = (promise: Promise<unknown>) =>
  promise.then(
    () => null,
    (error: unknown) => (error instanceof PreviewsApiError ? error.code : "other"),
  );

test("list parses the contract's body", async () => {
  const rows = [
    preview(),
    exited(),
    preview({ state: "starting", listener_pid: null }),
    preview({ state: "port_taken", listener_pid: null, worktree_id: null }),
    exited({ state: "stopped", exit_code: null }),
  ];
  const fetchImpl = reply({ previews: rows });
  expect(await createPreviewsApi("tok", fetchImpl).list(W)).toEqual(rows);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/previews`);
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual(AUTH);
  expect(init.body).toBeUndefined();
});

test("start posts the body and parses the preview", async () => {
  const fetchImpl = reply({ preview: preview({ state: "starting", listener_pid: null }) }, 201);
  const body = { worktree_id: null, command: "npm run dev", port: 5173 };
  const answer = await createPreviewsApi("tok", fetchImpl).start(W, body);
  expect(answer.state).toBe("starting");
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/previews`);
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual(JSON_HEADERS);
  expect(JSON.parse(init.body as string)).toEqual(body);
});

test("stop posts an empty body and parses the preview", async () => {
  const stopped = exited({ state: "stopped", exit_code: null });
  const fetchImpl = reply({ preview: stopped });
  expect(await createPreviewsApi("tok", fetchImpl).stop(P)).toEqual(stopped);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/previews/${P}/stop`);
  expect(init.method).toBe("POST");
  expect(init.body).toBe("{}");
});

test("open and remove need their literal true", async () => {
  const opened = reply({ opened: true });
  await createPreviewsApi("tok", opened).open(P);
  expect(call(opened)[0]).toBe(`/api/v2/previews/${P}/open`);
  expect(call(opened)[1].body).toBe("{}");
  const removed = reply({ removed: true });
  await createPreviewsApi("tok", removed).remove(P);
  expect(call(removed)[0]).toBe(`/api/v2/previews/${P}/remove`);
  expect(call(removed)[1].method).toBe("POST");
  expect(await code(createPreviewsApi("tok", reply({ opened: false })).open(P))).toBe(
    "unavailable",
  );
  expect(await code(createPreviewsApi("tok", reply({ opened: 1 })).open(P))).toBe("unavailable");
  expect(await code(createPreviewsApi("tok", reply({ removed: "true" })).remove(P))).toBe(
    "unavailable",
  );
  expect(await code(createPreviewsApi("tok", reply({})).remove(P))).toBe("unavailable");
});

test("output always sends after and parses the answer", async () => {
  const body = {
    preview: preview(),
    start: 0,
    next: 5,
    text: "hello",
    dropped: false,
    done: false,
  };
  const fetchImpl = reply(body);
  expect(await createPreviewsApi("tok", fetchImpl).output(P, 0)).toEqual(body);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/previews/${P}/output?after=0`);
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual(AUTH);
});

test("an output with start after next, or a bad field, is unavailable", async () => {
  const base = { preview: preview(), start: 0, next: 5, text: "", dropped: false, done: false };
  for (const bad of [
    { ...base, start: 6 },
    { ...base, text: 1 },
    { ...base, dropped: "no" },
    { ...base, done: 0 },
    { ...base, next: -1 },
    { ...base, preview: preview({ port: 80 }) },
  ])
    expect(await code(createPreviewsApi("tok", reply(bad)).output(P, 0))).toBe("unavailable");
});

test("a non-UUID id fails before any fetch", async () => {
  const fetchImpl = reply({});
  const api = createPreviewsApi("tok", fetchImpl);
  const body = { worktree_id: null, command: "x", port: 5173 };
  expect(await code(api.list("nope"))).toBe("invalid_workspace");
  expect(await code(api.start("nope", body))).toBe("invalid_workspace");
  expect(await code(api.start(W, { ...body, worktree_id: "nope" }))).toBe("invalid_workspace");
  expect(await code(api.stop("nope"))).toBe("invalid_workspace");
  expect(await code(api.open("nope"))).toBe("invalid_workspace");
  expect(await code(api.remove("nope"))).toBe("invalid_workspace");
  expect(await code(api.output("nope", 0))).toBe("invalid_workspace");
  expect(await code(api.output(P, -1))).toBe("invalid_query");
  expect(await code(api.output(P, 1.5))).toBe("invalid_query");
  expect(fetchImpl).not.toHaveBeenCalled();
});

test.each([
  ["a bad preview id", preview({ preview_id: "x" })],
  ["a bad workspace id", preview({ workspace_id: "x" })],
  ["a bad worktree id", preview({ worktree_id: "x" })],
  ["a missing command", preview({ command: 1 })],
  ["an unknown state", preview({ state: "running" })],
  ["a port below 1024", preview({ port: 1023 })],
  ["a port above 65535", preview({ port: 65536 })],
  ["a fractional port", preview({ port: 5173.5 })],
  ["a zero pid", preview({ pid: 0 })],
  ["a string pid", preview({ pid: "1" })],
  ["a listening preview without a listener", preview({ listener_pid: null })],
  ["a zero listener", preview({ listener_pid: 0 })],
  ["a listener while starting", preview({ state: "starting" })],
  ["a listener when port_taken", preview({ state: "port_taken" })],
  ["a listener once exited", exited({ listener_pid: 4300 })],
  ["an exit code while live", preview({ exit_code: 0 })],
  ["an exit code when stopped", exited({ state: "stopped", exit_code: 0 })],
  ["an exited preview without a code", exited({ exit_code: null })],
  ["an exit code below the range", exited({ exit_code: -2_147_483_649 })],
  ["an exit code above the range", exited({ exit_code: 4_294_967_296 })],
  ["a fractional exit code", exited({ exit_code: 1.5 })],
  ["a short start time", preview({ started_at: "now" })],
  ["an end while live", preview({ ended_at: TIME })],
  ["no end once ended", exited({ ended_at: null })],
  ["a short end time", exited({ ended_at: "later" })],
])("%s is unavailable", async (_name, bad) => {
  expect(await code(createPreviewsApi("tok", reply({ previews: [bad] })).list(W))).toBe(
    "unavailable",
  );
});

test("the exit code range is inclusive", async () => {
  for (const exit_code of [-2_147_483_648, 4_294_967_295, 0]) {
    const rows = [exited({ exit_code })];
    expect(await createPreviewsApi("tok", reply({ previews: rows })).list(W)).toEqual(rows);
  }
  const edge = [preview({ port: 1024 }), preview({ port: 65535 })];
  expect(await createPreviewsApi("tok", reply({ previews: edge })).list(W)).toEqual(edge);
});

test("a refusal carries its code; anything else is unavailable", async () => {
  const refused = reply({ error: { code: "port_taken", message: "x" } }, 409);
  expect(await code(createPreviewsApi("tok", refused).open(P))).toBe("port_taken");
  expect(await code(createPreviewsApi("tok", reply({}, 500)).stop(P))).toBe("unavailable");
  const text = vi.fn(async () => new Response("<html>", { status: 502 })) as typeof fetch;
  expect(await code(createPreviewsApi("tok", text).list(W))).toBe("unavailable");
});

test("a missing list array is unavailable", async () => {
  expect(await code(createPreviewsApi("tok", reply({})).list(W))).toBe("unavailable");
});

test("start and open wait 30 s; the rest 15 s", async () => {
  vi.useFakeTimers();
  try {
    const hang = () =>
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => reject(new Error("aborted")));
          }),
      ) as unknown as typeof fetch;
    const body = { worktree_id: null, command: "x", port: 5173 };
    const settled = (promise: Promise<unknown>) => {
      let done = false;
      promise.then(
        () => {
          done = true;
        },
        () => {
          done = true;
        },
      );
      return () => done;
    };
    const stop = settled(createPreviewsApi("tok", hang()).stop(P));
    const start = settled(createPreviewsApi("tok", hang()).start(W, body));
    const open = settled(createPreviewsApi("tok", hang()).open(P));
    await vi.advanceTimersByTimeAsync(14_999);
    expect([stop(), start(), open()]).toEqual([false, false, false]);
    await vi.advanceTimersByTimeAsync(1);
    expect([stop(), start(), open()]).toEqual([true, false, false]);
    await vi.advanceTimersByTimeAsync(15_000);
    expect([start(), open()]).toEqual([true, true]);
  } finally {
    vi.useRealTimers();
  }
});

test("an aborted signal fails before any fetch and aborts the request", async () => {
  const fetchImpl = reply({ previews: [] });
  const stopped = new AbortController();
  stopped.abort();
  await expect(createPreviewsApi("tok", fetchImpl).list(W, stopped.signal)).rejects.toThrow(
    "cancelled",
  );
  expect(fetchImpl).not.toHaveBeenCalled();

  const live = new AbortController();
  const hanging = vi.fn(
    (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  ) as unknown as typeof fetch;
  const pending = createPreviewsApi("tok", hanging).list(W, live.signal);
  live.abort();
  await expect(pending).rejects.toThrow("aborted");
});

/** The body as the server writes it: every non-ASCII UTF-16 unit as a \\uXXXX escape. */
const asServerBody = (value: unknown) =>
  JSON.stringify(value).replace(
    /[^\x20-\x7e]/g,
    (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
const serverReply = (value: unknown) =>
  vi.fn(async () => new Response(asServerBody(value))) as typeof fetch;
const WORST = "😀".repeat(8000);

test("a start answer with the longest command escaped parses", async () => {
  const fetchImpl = serverReply({
    preview: preview({ state: "starting", listener_pid: null, command: WORST }),
  });
  const body = { worktree_id: null, command: WORST, port: 5173 };
  expect((await createPreviewsApi("tok", fetchImpl).start(W, body)).command).toBe(WORST);
});

test("a list of six previews with the longest command escaped parses", async () => {
  const rows = [
    preview({ command: WORST }),
    preview({ command: WORST }),
    exited({ command: WORST }),
    exited({ command: WORST }),
    exited({ command: WORST }),
    exited({ command: WORST }),
  ];
  expect(await createPreviewsApi("tok", serverReply({ previews: rows })).list(W)).toEqual(rows);
});

test("an answer over its bound is unavailable", async () => {
  const over = (value: unknown) => vi.fn(async () => new Response(JSON.stringify(value)));
  const big = (kib: number) => "x".repeat(kib * 1024);
  expect(
    await code(
      createPreviewsApi("tok", over({ previews: [], pad: big(1024) }) as typeof fetch).list(W),
    ),
  ).toBe("unavailable");
  expect(
    await code(
      createPreviewsApi("tok", over({ opened: true, pad: big(128) }) as typeof fetch).open(P),
    ),
  ).toBe("unavailable");
  expect(
    await code(
      createPreviewsApi(
        "tok",
        over({
          preview: preview(),
          start: 0,
          next: 0,
          text: big(2048),
          dropped: false,
          done: false,
        }) as typeof fetch,
      ).output(P, 0),
    ),
  ).toBe("unavailable");
});
