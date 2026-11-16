/**
 * Unit tests pinning api.ts behaviour that api.accept.test.ts leaves implicit: the default
 * `fetchImpl` parameter actually reaches the global `fetch`, RestError's shape, and the 30 s
 * abort every GET carries.
 */
import { afterEach, expect, test, vi } from "vitest";
import { createRest, RestError } from "./api";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.useRealTimers();
  globalThis.fetch = originalFetch;
});

test("createRest falls back to the global fetch when no fetchImpl is given", async () => {
  const calls: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    calls.push(String(input));
    return new Response(JSON.stringify({ sessions: [], state: "ready", reason: "", omitted: 0 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  await createRest("tok").sessions();
  expect(calls).toEqual(["/api/sessions"]);
});

test("RestError is a real Error with a stable name, distinguishable from other rejections", () => {
  const error = new RestError(404, "not found");
  expect(error).toBeInstanceOf(Error);
  expect(error.name).toBe("RestError");
  expect(error.message).toBe("REST 404");
  expect(error.body).toBe("not found");
  expect(error.status).toBe(404);
});

/** A fetch that never answers, and aborts the way the real one does when its signal fires. */
function hungFetch() {
  const signals: AbortSignal[] = [];
  const impl = ((_input: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) throw new Error("the GET carried no AbortSignal");
      signals.push(signal);
      signal.addEventListener("abort", () => reject(signal.reason));
    })) as typeof fetch;
  return { impl, signals };
}

test("a hung GET rejects after the 30 s timeout instead of pending for the session", async () => {
  // Without this a server that accepts the request and never answers leaves the rail reading
  // "loading sessions…" until the window is closed: no note can recover a promise that never
  // settles, and the store has nothing to report.
  vi.useFakeTimers();
  const { impl, signals } = hungFetch();
  const outcome = createRest("tok", impl)
    .sessions()
    .then(
      () => "resolved" as const,
      (error: unknown) => error,
    );

  await vi.advanceTimersByTimeAsync(29_999);
  expect(signals[0]?.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);

  expect(signals[0]?.aborted).toBe(true);
  expect(await outcome).toBeInstanceOf(Error);
});

test("a GET that answers in time cancels its abort timer, so a later tick aborts nothing", async () => {
  vi.useFakeTimers();
  const signals: AbortSignal[] = [];
  const impl = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.signal) signals.push(init.signal);
    return new Response(JSON.stringify({ hits: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  await createRest("tok", impl).search("milk");
  await vi.advanceTimersByTimeAsync(60_000);
  expect(signals[0]?.aborted).toBe(false);
});

test("search() carries the request id as its own query parameter, and omits it when there is none", async () => {
  const calls: string[] = [];
  const impl = (async (input: RequestInfo | URL) => {
    calls.push(String(input));
    return new Response(JSON.stringify({ hits: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  const rest = createRest("tok-secret", impl);
  await rest.search("buy milk", "3f1d0c6a-0000-4000-8000-000000000001");
  await rest.search("buy milk");
  expect(calls).toEqual([
    `/api/search?q=${encodeURIComponent("buy milk")}&request_id=3f1d0c6a-0000-4000-8000-000000000001`,
    `/api/search?q=${encodeURIComponent("buy milk")}`,
  ]);
  for (const url of calls) expect(url).not.toContain("tok-secret");
});
