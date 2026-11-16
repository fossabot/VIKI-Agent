import { expect, test, vi } from "vitest";
import { createTerminalApi, TerminalApiError } from "./terminalApi";

const W = "22222222-2222-4222-8222-222222222222";
const T = "33333333-3333-4333-8333-333333333333";

function reply(body: unknown, status = 200): typeof fetch {
  return vi.fn(async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
}
const code = (promise: Promise<unknown>) =>
  promise.then(
    () => null,
    (error: unknown) => (error instanceof TerminalApiError ? error.code : "other"),
  );

test("open posts the worktree with the token", async () => {
  const fetchImpl = reply({ opened: true });
  await createTerminalApi("tok", fetchImpl).open(W, T);
  const [url, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
  expect(url).toBe(`/api/v2/code/${W}/terminal`);
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({
    authorization: "Bearer tok",
    "content-type": "application/json",
  });
  expect(JSON.parse(init.body as string)).toEqual({ worktree_id: T });
});

test("open sends a null worktree for the folder itself", async () => {
  const fetchImpl = reply({ opened: true });
  await createTerminalApi("tok", fetchImpl).open(W, null);
  const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
  expect(JSON.parse(init.body as string)).toEqual({ worktree_id: null });
});

test("a malformed id never reaches fetch", async () => {
  const fetchImpl = reply({ opened: true });
  const api = createTerminalApi("tok", fetchImpl);
  expect(await code(api.open("nope", null))).toBe("invalid_workspace");
  expect(await code(api.open(W, "nope"))).toBe("invalid_workspace");
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("only exactly {opened: true} is an answer", async () => {
  for (const body of [{}, { opened: false }, { opened: "true" }, { opened: true, extra: 1 }, []]) {
    expect(await code(createTerminalApi("tok", reply(body)).open(W, null))).toBe("unavailable");
  }
});

test("a refusal carries its code, else unavailable", async () => {
  const refused = reply({ error: { code: "worktree_not_ready", message: "x" } }, 409);
  expect(await code(createTerminalApi("tok", refused).open(W, null))).toBe("worktree_not_ready");
  expect(await code(createTerminalApi("tok", reply({}, 500)).open(W, null))).toBe("unavailable");
});

test("an answer over 64 KiB is unavailable", async () => {
  const big = reply({ opened: true, pad: "x".repeat(70 * 1024) });
  expect(await code(createTerminalApi("tok", big).open(W, null))).toBe("unavailable");
});

test("an aborted signal never reaches fetch", async () => {
  const fetchImpl = reply({ opened: true });
  const controller = new AbortController();
  controller.abort();
  await expect(
    createTerminalApi("tok", fetchImpl).open(W, null, controller.signal),
  ).rejects.toThrow("cancelled");
  expect(fetchImpl).not.toHaveBeenCalled();
});
