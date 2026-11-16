import { expect, test, vi } from "vitest";
import { createReviewApi, ReviewApiError } from "./reviewApi";

const W = "22222222-2222-4222-8222-222222222222";
const T = "33333333-3333-4333-8333-333333333333";
const OID = "a".repeat(40);
const DIGEST = "d".repeat(64);
const AUTH = { authorization: "Bearer tok" };
const JSON_HEADERS = { ...AUTH, "content-type": "application/json" };

const integration = (over: Record<string, unknown> = {}) => ({
  target_branch: "main",
  target_oid: OID,
  merge_base: OID,
  fast_forward: true,
  ahead: 1,
  behind: 0,
  commits: [{ oid: OID, subject: "Add x" }],
  commits_truncated: false,
  paths: [{ path: "a.ts", change: "added", kind: "text" }],
  paths_truncated: false,
  ...over,
});
const review = (over: Record<string, unknown> = {}) => ({
  workspace_id: W,
  worktree_id: T,
  branch: "viki/x",
  head_oid: OID,
  in_progress: false,
  entries: [
    { path: "a.ts", change: "added", kind: "text", digest: DIGEST },
    { path: "b.bin", change: "modified", kind: "binary" },
  ],
  truncated: false,
  remote: "origin",
  remote_state: "ready",
  remote_oid: OID,
  pull_request_url: "https://github.com/o/r/compare/x",
  integration: integration(),
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
    (error: unknown) => (error instanceof ReviewApiError ? error.code : "other"),
  );

test("review parses the contract's body", async () => {
  const fetchImpl = reply(review());
  expect(await createReviewApi("tok", fetchImpl).review(W, T)).toEqual(review());
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/review?worktree=${T}`);
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual(AUTH);
  expect(init.body).toBeUndefined();
});

test("review without a worktree sends no query and accepts nulls", async () => {
  const body = review({
    worktree_id: null,
    branch: null,
    remote: null,
    remote_state: "none",
    remote_oid: null,
    pull_request_url: null,
    integration: null,
    entries: [],
  });
  const fetchImpl = reply(body);
  expect(await createReviewApi("tok", fetchImpl).review(W, null)).toEqual(body);
  expect(call(fetchImpl)[0]).toBe(`/api/v2/code/${W}/review`);
});

test("review rejects each bad field", async () => {
  const bad: Record<string, unknown>[] = [
    { workspace_id: "nope" },
    { worktree_id: "nope" },
    { head_oid: "abc" },
    { head_oid: null },
    { head_oid: "A".repeat(40) },
    { in_progress: "no" },
    { truncated: 0 },
    { entries: null },
    { entries: [{ path: "a", change: "renamed", kind: "text", digest: DIGEST }] },
    { entries: [{ path: "a", change: "added", kind: "weird" }] },
    { entries: [{ path: "a", change: "added", kind: "text" }] },
    { entries: [{ path: "a", change: "added", kind: "text", digest: "d".repeat(63) }] },
    { entries: [{ path: "a", change: "added", kind: "binary", digest: DIGEST }] },
    { remote: "-x" },
    { remote: "a".repeat(101) },
    { remote_state: "other" },
    { remote_oid: "xyz" },
    { pull_request_url: "http://github.com/o/r" },
    { pull_request_url: "https://evil.example/github.com/x" },
    { pull_request_url: "https://github.com/" },
    { integration: integration({ ahead: -1 }) },
    { integration: integration({ behind: 1.5 }) },
    { integration: integration({ ahead: Number.MAX_SAFE_INTEGER + 1 }) },
    { integration: integration({ fast_forward: 1 }) },
    { integration: integration({ target_oid: "abc" }) },
    { integration: integration({ target_oid: null }) },
    { integration: integration({ commits: [{ oid: "abc", subject: "s" }] }) },
    { integration: integration({ commits: [{ oid: OID, subject: 1 }] }) },
    { integration: integration({ paths: [{ path: "a", change: "added", kind: "x" }] }) },
    { integration: integration({ paths_truncated: "no" }) },
    { integration: "yes" },
  ];
  for (const over of bad)
    expect(await code(createReviewApi("t", reply(review(over))).review(W, T))).toBe("unavailable");
  expect(await code(createReviewApi("t", reply([])).review(W, T))).toBe("unavailable");
});

test("a 64-hex oid and a gitlab page are accepted", async () => {
  const body = review({
    head_oid: "b".repeat(64),
    pull_request_url: "https://gitlab.com/o/r/-/merge_requests/new",
  });
  expect(await createReviewApi("t", reply(body)).review(W, null)).toEqual(body);
});

test("view orders path, worktree and range", async () => {
  const body = { path: "a b/ü.ts", diff: "@@", content: null, reason: null };
  const fetchImpl = reply(body);
  const api = createReviewApi("tok", fetchImpl);
  expect(await api.view(W, "a b/ü.ts", { worktreeId: T, integration: true })).toEqual(body);
  const [url, init] = call(fetchImpl);
  const query = new URLSearchParams({ path: "a b/ü.ts", worktree: T, range: "integration" });
  expect(url).toBe(`/api/v2/code/${W}/review/file?${query}`);
  expect(url.indexOf("path=")).toBeLessThan(url.indexOf("worktree="));
  expect(url.indexOf("worktree=")).toBeLessThan(url.indexOf("range=integration"));
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual(AUTH);

  const plain = reply({ path: "a", diff: null, content: "x", reason: "no_diff" });
  await createReviewApi("tok", plain).view(W, "a", { worktreeId: null, integration: false });
  expect(call(plain)[0]).toBe(`/api/v2/code/${W}/review/file?path=a`);
});

test("view rejects a bad field", async () => {
  const good = { path: "a", diff: null, content: null, reason: null };
  for (const over of [{ path: 1 }, { diff: 1 }, { content: 1 }, { reason: "gone" }]) {
    const api = createReviewApi("t", reply({ ...good, ...over }));
    expect(await code(api.view(W, "a", { worktreeId: null, integration: false }))).toBe(
      "unavailable",
    );
  }
});

const commitBody = {
  policy_revision: 3,
  root_physical_id: "1:2",
  worktree_id: T,
  branch: "viki/x",
  head_oid: OID,
  message: "m",
  paths: [{ path: "a.ts", digest: DIGEST }],
};

test("commit posts its body and parses the answer", async () => {
  const answer = { outcome: "committed", head_oid: OID, hook_changed: ["a.ts"], unexpected: [] };
  const fetchImpl = reply(answer);
  expect(await createReviewApi("tok", fetchImpl).commit(W, commitBody)).toEqual(answer);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/commit`);
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual(JSON_HEADERS);
  expect(JSON.parse(init.body as string)).toEqual(commitBody);
});

test("commit rejects a bad answer", async () => {
  const good = { outcome: "committed", head_oid: OID, hook_changed: [], unexpected: [] };
  for (const over of [
    { outcome: "done" },
    { head_oid: "abc" },
    { hook_changed: "a" },
    { unexpected: [1] },
  ])
    expect(
      await code(createReviewApi("t", reply({ ...good, ...over })).commit(W, commitBody)),
    ).toBe("unavailable");
});

test("integrate posts to the worktree's route", async () => {
  const body = {
    policy_revision: 3,
    root_physical_id: "1:2",
    target_branch: "main",
    target_oid: OID,
    head_oid: OID,
  };
  const fetchImpl = reply({ outcome: "integrated", head_oid: OID });
  expect(await createReviewApi("tok", fetchImpl).integrate(T, body)).toEqual({
    outcome: "integrated",
    head_oid: OID,
  });
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/worktrees/${T}/integrate`);
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual(JSON_HEADERS);
  expect(JSON.parse(init.body as string)).toEqual(body);
  for (const bad of [
    { outcome: "no", head_oid: OID },
    { outcome: "integrated", head_oid: "x" },
  ])
    expect(await code(createReviewApi("t", reply(bad)).integrate(T, body))).toBe("unavailable");
});

test("push and check post to their routes and parse outcomes", async () => {
  const push = {
    policy_revision: 3,
    root_physical_id: "1:2",
    remote: "origin",
    branch: "b",
    head_oid: OID,
  };
  const pushFetch = reply({ outcome: "unknown", remote_oid: null });
  expect(await createReviewApi("tok", pushFetch).push(W, push)).toEqual({
    outcome: "unknown",
    remote_oid: null,
  });
  expect(call(pushFetch)[0]).toBe(`/api/v2/code/${W}/push`);
  expect(call(pushFetch)[1].method).toBe("POST");
  expect(JSON.parse(call(pushFetch)[1].body as string)).toEqual(push);

  const check = { remote: "origin", branch: "b", head_oid: OID };
  const checkFetch = reply({ outcome: "not_pushed", remote_oid: OID });
  expect(await createReviewApi("tok", checkFetch).check(W, check)).toEqual({
    outcome: "not_pushed",
    remote_oid: OID,
  });
  expect(call(checkFetch)[0]).toBe(`/api/v2/code/${W}/push/check`);
  expect(call(checkFetch)[1].headers).toEqual(JSON_HEADERS);
  expect(JSON.parse(call(checkFetch)[1].body as string)).toEqual(check);

  // each set is its own: push has no not_pushed, check has no unknown
  expect(
    await code(
      createReviewApi("t", reply({ outcome: "not_pushed", remote_oid: null })).push(W, push),
    ),
  ).toBe("unavailable");
  expect(
    await code(
      createReviewApi("t", reply({ outcome: "unknown", remote_oid: null })).check(W, check),
    ),
  ).toBe("unavailable");
  expect(
    await code(createReviewApi("t", reply({ outcome: "pushed", remote_oid: "zz" })).push(W, push)),
  ).toBe("unavailable");
});

test("a refusal answers its error code, anything else unavailable", async () => {
  const refuse = (body: unknown, status: number) => reply(body, status);
  const api = (fetchImpl: typeof fetch) => createReviewApi("t", fetchImpl);
  expect(
    await code(api(refuse({ error: { code: "stale_review", message: "x" } }, 409)).review(W, null)),
  ).toBe("stale_review");
  expect(await code(api(refuse({ error: {} }, 500)).review(W, null))).toBe("unavailable");
  expect(await code(api(refuse({ error: { code: "" } }, 409)).review(W, null))).toBe("unavailable");
  const html = vi.fn(async () => new Response("<html>", { status: 502 })) as typeof fetch;
  expect(await code(api(html).review(W, null))).toBe("unavailable");
});

test("a non-UUID id is refused before any fetch", async () => {
  const fetchImpl = reply({});
  const api = createReviewApi("t", fetchImpl);
  const view = { worktreeId: null, integration: false };
  const results = await Promise.all([
    code(api.review("nope", null)),
    code(api.review(W, "nope")),
    code(api.view("nope", "a", view)),
    code(api.view(W, "a", { worktreeId: "nope", integration: false })),
    code(api.commit("nope", commitBody)),
    code(api.commit(W, { ...commitBody, worktree_id: "nope" })),
    code(api.integrate("nope", {} as never)),
    code(api.push("nope", {} as never)),
    code(api.check("nope", {} as never)),
  ]);
  expect(results.every((result) => result === "invalid_workspace")).toBe(true);
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("an aborted signal stops the call", async () => {
  const fetchImpl = reply(review());
  const controller = new AbortController();
  controller.abort();
  await expect(createReviewApi("t", fetchImpl).review(W, null, controller.signal)).rejects.toThrow(
    "cancelled",
  );
  expect(fetchImpl).not.toHaveBeenCalled();

  const live = new AbortController();
  const pending = vi.fn(
    (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  ) as unknown as typeof fetch;
  const running = createReviewApi("t", pending).review(W, null, live.signal);
  live.abort();
  await expect(running).rejects.toThrow("aborted");
});
