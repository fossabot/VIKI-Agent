import { expect, test, vi } from "vitest";
import { ChecksApiError, createChecksApi } from "./checksApi";

const W = "22222222-2222-4222-8222-222222222222";
const T = "33333333-3333-4333-8333-333333333333";
const C = "44444444-4444-4444-8444-444444444444";
const R = "55555555-5555-4555-8555-555555555555";
const OID = "a".repeat(40);
const AUTH = { authorization: "Bearer tok" };
const JSON_HEADERS = { ...AUTH, "content-type": "application/json" };
const TIME = "2026-10-04T01:02:03.456Z";

const run = (over: Record<string, unknown> = {}) => ({
  run_id: R,
  check_id: C,
  workspace_id: W,
  worktree_id: T,
  head_oid: OID,
  dirty: false,
  outcome: "failed",
  exit_code: 2,
  duration_ms: 1500,
  output_bytes: 10,
  started_at: TIME,
  ended_at: TIME,
  ...over,
});
const check = (over: Record<string, unknown> = {}) => ({
  check_id: C,
  name: "tests",
  command: "npm test",
  timeout_s: 600,
  created_at: TIME,
  readable: true,
  last_run: run(),
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
    (error: unknown) => (error instanceof ChecksApiError ? error.code : "other"),
  );

test("list parses the contract's body", async () => {
  const fetchImpl = reply({
    checks: [check(), check({ readable: false, name: null, command: null, last_run: null })],
  });
  const items = await createChecksApi("tok", fetchImpl).list(W, T);
  expect(items).toEqual([
    check(),
    check({ readable: false, name: null, command: null, last_run: null }),
  ]);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/checks?worktree=${T}`);
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual(AUTH);
  expect(init.body).toBeUndefined();
});

test("list without a worktree sends no query", async () => {
  const fetchImpl = reply({ checks: [] });
  expect(await createChecksApi("tok", fetchImpl).list(W, null)).toEqual([]);
  expect(call(fetchImpl)[0]).toBe(`/api/v2/code/${W}/checks`);
});

test("add posts the body and parses the check", async () => {
  const fetchImpl = reply({ check: check({ last_run: null }) }, 201);
  const body = { name: "tests", command: "npm test", timeout_s: 600 };
  expect(await createChecksApi("tok", fetchImpl).add(W, body)).toEqual(check({ last_run: null }));
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/checks`);
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual(JSON_HEADERS);
  expect(JSON.parse(init.body as string)).toEqual(body);
});

test("remove posts an empty body and needs deleted true", async () => {
  const fetchImpl = reply({ deleted: true });
  await createChecksApi("tok", fetchImpl).remove(C);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/checks/${C}/delete`);
  expect(init.method).toBe("POST");
  expect(init.body).toBe("{}");
  expect(await code(createChecksApi("tok", reply({ deleted: false })).remove(C))).toBe(
    "unavailable",
  );
});

test("run posts the worktree and parses the run", async () => {
  const fetchImpl = reply(
    { run: run({ outcome: "running", exit_code: null, ended_at: null }) },
    201,
  );
  const answer = await createChecksApi("tok", fetchImpl).run(C, null);
  expect(answer.outcome).toBe("running");
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/checks/${C}/runs`);
  expect(JSON.parse(init.body as string)).toEqual({ worktree_id: null });
  const withTree = reply({ run: run() }, 201);
  await createChecksApi("tok", withTree).run(C, T);
  expect(JSON.parse(call(withTree)[1].body as string)).toEqual({ worktree_id: T });
});

test("runs lists the runs", async () => {
  const fetchImpl = reply({ runs: [run(), run({ head_oid: null, dirty: null })] });
  expect(await createChecksApi("tok", fetchImpl).runs(C)).toHaveLength(2);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/checks/${C}/runs`);
  expect(init.method).toBe("GET");
});

test("cancel posts an empty body", async () => {
  const fetchImpl = reply({ run: run({ outcome: "cancelled", exit_code: null }) });
  expect((await createChecksApi("tok", fetchImpl).cancel(R)).outcome).toBe("cancelled");
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/check-runs/${R}/cancel`);
  expect(init.body).toBe("{}");
});

test("output always sends after and parses the answer", async () => {
  const body = { run: run(), start: 0, next: 5, text: "hello", dropped: false, done: true };
  const fetchImpl = reply(body);
  expect(await createChecksApi("tok", fetchImpl).output(R, 0)).toEqual(body);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/check-runs/${R}/output?after=0`);
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual(AUTH);
});

test("a non-UUID id fails before any fetch", async () => {
  const fetchImpl = reply({});
  const api = createChecksApi("tok", fetchImpl);
  expect(await code(api.list("nope", null))).toBe("invalid_workspace");
  expect(await code(api.list(W, "nope"))).toBe("invalid_workspace");
  expect(await code(api.run("nope", null))).toBe("invalid_workspace");
  expect(await code(api.output(R, -1))).toBe("invalid_query");
  expect(fetchImpl).not.toHaveBeenCalled();
});

test.each([
  ["a bad outcome", run({ outcome: "ok" })],
  ["a bad head", run({ head_oid: "abc" })],
  ["dirty with a null head", run({ head_oid: null, dirty: true })],
  ["a null dirty with a head", run({ dirty: null })],
  ["a negative duration", run({ duration_ms: -1 })],
  ["a short time", run({ started_at: "now" })],
  ["a bad run id", run({ run_id: "x" })],
])("a run with %s is unavailable", async (_name, bad) => {
  expect(
    await code(createChecksApi("tok", reply({ checks: [check({ last_run: bad })] })).list(W, null)),
  ).toBe("unavailable");
});

test.each([
  ["a readable check without a name", check({ name: null })],
  ["an unreadable check with a name", check({ readable: false })],
  ["a timeout below 60", check({ timeout_s: 59 })],
  ["a timeout above 3600", check({ timeout_s: 3601 })],
  ["a non-boolean readable", check({ readable: 1 })],
])("%s is unavailable", async (_name, bad) => {
  expect(await code(createChecksApi("tok", reply({ checks: [bad] })).list(W, null))).toBe(
    "unavailable",
  );
});

test("an output with start after next is unavailable", async () => {
  const body = { run: run(), start: 6, next: 5, text: "", dropped: false, done: false };
  expect(await code(createChecksApi("tok", reply(body)).output(R, 0))).toBe("unavailable");
});

test("a refusal carries its code; anything else is unavailable", async () => {
  const refused = reply({ error: { code: "check_running", message: "x" } }, 409);
  expect(await code(createChecksApi("tok", refused).run(C, null))).toBe("check_running");
  expect(await code(createChecksApi("tok", reply({}, 500)).run(C, null))).toBe("unavailable");
  const text = vi.fn(async () => new Response("<html>", { status: 502 })) as typeof fetch;
  expect(await code(createChecksApi("tok", text).list(W, null))).toBe("unavailable");
});

test("a missing list array is unavailable", async () => {
  expect(await code(createChecksApi("tok", reply({})).list(W, null))).toBe("unavailable");
});

test("an aborted signal fails before any fetch and aborts the request", async () => {
  const fetchImpl = reply({ checks: [] });
  const stopped = new AbortController();
  stopped.abort();
  await expect(createChecksApi("tok", fetchImpl).list(W, null, stopped.signal)).rejects.toThrow(
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
  const pending = createChecksApi("tok", hanging).list(W, null, live.signal);
  live.abort();
  await expect(pending).rejects.toThrow("aborted");
});
