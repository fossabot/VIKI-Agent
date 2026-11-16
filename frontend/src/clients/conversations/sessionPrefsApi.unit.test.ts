import { afterEach, expect, test, vi } from "vitest";
import { createSessionPrefsApi } from "./sessionPrefsApi";

afterEach(() => {
  vi.useRealTimers();
});

const ID = "a1111111-1111-4111-8111-111111111111";
const ok = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** A fetch that never answers, and aborts the way the real one does when its signal fires
 * (api.unit.test.ts's own `hungFetch`). */
function hungFetch(): { impl: typeof fetch; signals: AbortSignal[] } {
  const signals: AbortSignal[] = [];
  const impl = ((_input: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) throw new Error("the request carried no AbortSignal");
      signals.push(signal);
      signal.addEventListener("abort", () => reject(signal.reason));
    })) as typeof fetch;
  return { impl, signals };
}

test("get sends a bearer GET with no body and returns the parsed prefs", async () => {
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${ID}/prefs`);
    expect(init?.method).toBe("GET");
    expect(init?.headers).toEqual({ authorization: "Bearer secret" });
    expect(init?.body).toBeUndefined();
    return ok({ model: "sonnet", effort: "high" });
  });
  const prefs = await createSessionPrefsApi("secret", fetchImpl).get(ID);
  expect(prefs).toEqual({ model: "sonnet", effort: "high" });
  expect(fetchImpl).toHaveBeenCalledOnce();
});

test("save sends a bearer PATCH whose body is only the given keys", async () => {
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${ID}/prefs`);
    expect(init?.method).toBe("PATCH");
    expect(init?.headers).toEqual({
      authorization: "Bearer secret",
      "content-type": "application/json",
    });
    expect(JSON.parse(String(init?.body))).toEqual({ model: "opus" });
    return ok({ model: "opus", effort: "high" });
  });
  const prefs = await createSessionPrefsApi("secret", fetchImpl).save(ID, { model: "opus" });
  expect(prefs).toEqual({ model: "opus", effort: "high" });
});

test("save with both fields sends both, and a null model round-trips on get", async () => {
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    expect(JSON.parse(String(init?.body))).toEqual({ model: "opus", effort: "max" });
    return ok({ model: null, effort: "max" });
  });
  const prefs = await createSessionPrefsApi("secret", fetchImpl).save(ID, {
    model: "opus",
    effort: "max",
  });
  expect(prefs).toEqual({ model: null, effort: "max" });
});

test("a non-canonical id rejects as invalid_input before any fetch, for get and save alike", async () => {
  const fetchImpl = vi.fn();
  const api = createSessionPrefsApi("secret", fetchImpl);
  await expect(api.get("not-a-uuid")).rejects.toThrow("invalid_input");
  await expect(api.get(ID.toUpperCase())).rejects.toThrow("invalid_input");
  await expect(api.get(ID.slice(0, -1))).rejects.toThrow("invalid_input"); // one char short
  await expect(api.save("not-a-uuid", { model: "x" })).rejects.toThrow("invalid_input");
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("a non-2xx response rejects as unavailable without reading the body", async () => {
  const fetchImpl = vi.fn(
    async () =>
      new Response(JSON.stringify({ error: { code: "session_exists" } }), { status: 409 }),
  );
  await expect(createSessionPrefsApi("secret", fetchImpl).get(ID)).rejects.toThrow("unavailable");
});

test.each<[string, unknown]>([
  ["an array", []],
  ["an extra key", { model: "x", effort: "high", mode: "default" }],
  ["a missing key", { model: "x" }],
  ["a non-string effort", { model: "x", effort: 1 }],
  ["a non-string, non-null model", { model: 1, effort: "high" }],
  ["an empty model", { model: "", effort: "high" }],
  ["a 1025-char model", { model: "x".repeat(1025), effort: "high" }],
  ["an empty effort", { model: "x", effort: "" }],
  ["a 33-char effort", { model: "x", effort: "x".repeat(33) }],
])("a malformed body (%s) rejects as invalid_response", async (_label, body) => {
  const fetchImpl = vi.fn(async () => ok(body));
  await expect(createSessionPrefsApi("secret", fetchImpl).get(ID)).rejects.toThrow(
    "invalid_response",
  );
});

test("a 1024-char model and a 32-char effort are accepted at the boundary", async () => {
  const model = "x".repeat(1024);
  const effort = "x".repeat(32);
  const fetchImpl = vi.fn(async () => ok({ model, effort }));
  await expect(createSessionPrefsApi("secret", fetchImpl).get(ID)).resolves.toEqual({
    model,
    effort,
  });
});

test("an already-aborted caller signal rejects as cancelled before any fetch", async () => {
  const fetchImpl = vi.fn();
  const controller = new AbortController();
  controller.abort();
  await expect(
    createSessionPrefsApi("secret", fetchImpl).get(ID, controller.signal),
  ).rejects.toThrow("cancelled");
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("aborting the caller signal mid-flight relays onto the fetch and rejects as cancelled", async () => {
  const { impl, signals } = hungFetch();
  const controller = new AbortController();
  const promise = createSessionPrefsApi("secret", impl).get(ID, controller.signal);
  const assertion = expect(promise).rejects.toThrow("cancelled");
  controller.abort();
  expect(signals[0]?.aborted).toBe(true);
  await assertion;
});

test("a fetch failure that is not an abort rejects as unavailable", async () => {
  const fetchImpl = vi.fn(async () => {
    throw new TypeError("network down");
  });
  await expect(createSessionPrefsApi("secret", fetchImpl).get(ID)).rejects.toThrow("unavailable");
});

test("a request that never answers aborts after 15s via setTimeout and rejects as unavailable", async () => {
  vi.useFakeTimers();
  const { impl, signals } = hungFetch();
  const outcome = createSessionPrefsApi("secret", impl)
    .get(ID)
    .then(
      () => "resolved" as const,
      (error: unknown) => error,
    );
  await vi.advanceTimersByTimeAsync(14_999);
  expect(signals[0]?.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(signals[0]?.aborted).toBe(true);
  const result = await outcome;
  expect(result).toBeInstanceOf(Error);
  expect((result as Error).message).toBe("unavailable");
});
