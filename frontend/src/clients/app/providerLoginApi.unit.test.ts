import { afterEach, expect, test, vi } from "vitest";
import { createProviderLoginApi, ProviderLoginApiError } from "./providerLoginApi";

afterEach(() => {
  vi.useRealTimers();
});

function reply(body: unknown, status = 200): typeof fetch {
  return vi.fn(async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
}
const code = (promise: Promise<unknown>) =>
  promise.then(
    () => null,
    (error: unknown) => (error instanceof ProviderLoginApiError ? error.code : "other"),
  );

test("open posts to the login route with the token, no body", async () => {
  const fetchImpl = reply({ opened: true });
  await createProviderLoginApi("tok", fetchImpl).open();
  const [url, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v2/provider/login");
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({ authorization: "Bearer tok" });
  expect(init.body).toBeUndefined();
});

test("only exactly {opened: true} is an answer", async () => {
  for (const body of [{}, { opened: false }, { opened: "true" }, { opened: true, extra: 1 }, []]) {
    expect(await code(createProviderLoginApi("tok", reply(body)).open())).toBe("unavailable");
  }
});

test("a refusal carries its code, else unavailable", async () => {
  const refused = reply({ error: { code: "provider_unavailable", message: "x" } }, 409);
  expect(await code(createProviderLoginApi("tok", refused).open())).toBe("provider_unavailable");
  expect(await code(createProviderLoginApi("tok", reply({}, 500)).open())).toBe("unavailable");
});

test("an answer over 64 KiB is unavailable", async () => {
  // Padded with trailing whitespace, not a second key: an otherwise-valid {opened: true} body
  // that JSON.parse would still accept as a 1-key success shape if the cap let it through, so
  // this only fails for the byte cap, never for the exact-shape check.
  const big = vi.fn(
    async () =>
      new Response(`${JSON.stringify({ opened: true })}${" ".repeat(70 * 1024)}`, { status: 200 }),
  ) as typeof fetch;
  expect(await code(createProviderLoginApi("tok", big).open())).toBe("unavailable");
});

test("the request aborts at the 15 s timeout, not before", async () => {
  vi.useFakeTimers();
  let signal: AbortSignal | undefined;
  const hung = ((_input: RequestInfo | URL, init?: RequestInit) => {
    signal = init?.signal ?? undefined;
    return new Promise<Response>(() => {});
  }) as typeof fetch;
  void createProviderLoginApi("tok", hung)
    .open()
    .catch(() => undefined);
  await vi.advanceTimersByTimeAsync(14_999);
  expect(signal?.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(signal?.aborted).toBe(true);
});

test("an aborted signal never reaches fetch", async () => {
  const fetchImpl = reply({ opened: true });
  const controller = new AbortController();
  controller.abort();
  await expect(createProviderLoginApi("tok", fetchImpl).open(controller.signal)).rejects.toThrow(
    "cancelled",
  );
  expect(fetchImpl).not.toHaveBeenCalled();
});
