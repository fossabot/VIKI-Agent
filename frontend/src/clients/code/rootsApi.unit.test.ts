import { expect, test, vi } from "vitest";
import { createRootsApi, RootsApiError, type SessionRootJson } from "./rootsApi";

const S = "11111111-1111-4111-8111-111111111111";
const W = "22222222-2222-4222-8222-222222222222";
const T = "33333333-3333-4333-8333-333333333333";
const U = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const additional = Array.from(
  { length: 7 },
  (_, index) => `${(index + 3).toString(16).repeat(8)}-3333-4333-8333-333333333333`,
);
const OID = "a".repeat(40);
const tree = (over: Record<string, unknown> = {}) => ({
  worktree_id: T,
  workspace_id: W,
  branch: "viki/x",
  base_oid: OID,
  path: "C:\\wt\\x",
  state: "ready",
  created_at: "2026-10-03T00:00:00Z",
  updated_at: "2026-10-03T00:00:00Z",
  ...over,
});
const archive = (over: Record<string, unknown> = {}) => ({
  worktree_id: T,
  oid: OID,
  archived_at: "2026-11-05T00:00:00.000Z",
  ...over,
});
const decision = (over: Record<string, unknown> = {}) => ({
  worktree_id: T,
  action: "archive",
  reason: "dirty",
  idle_since: "2026-11-01T00:00:00.000Z",
  eligible_at: "2026-11-02T00:00:00.000Z",
  bytes: 1024,
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
    (error: unknown) => (error instanceof RootsApiError ? error.code : "other"),
  );

test("roots sends a Bearer GET and parses rows", async () => {
  const row = { session_id: S, workspace_id: W, worktree_id: null };
  const fetchImpl = reply({ roots: [row], truncated: false });
  expect(await createRootsApi("tok", fetchImpl).roots()).toEqual({
    roots: [row],
    truncated: false,
  });
  const [url, init] = call(fetchImpl);
  expect(url).toBe("/api/v2/session-roots");
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual({ authorization: "Bearer tok" });
  expect(init.body).toBeUndefined();
});

test("record posts exactly the three fields", async () => {
  const row = { session_id: S, workspace_id: W, worktree_id: T };
  const fetchImpl = reply({ root: row }, 201);
  const sent = { ...row, extra: 1 } as typeof row;
  expect(await createRootsApi("tok", fetchImpl).record(sent)).toEqual(row);
  const [url, init] = call(fetchImpl);
  expect(url).toBe("/api/v2/session-roots");
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({
    authorization: "Bearer tok",
    "content-type": "application/json",
  });
  expect(JSON.parse(init.body as string)).toEqual(row);
});

test("roots parses additional folder IDs in selected order and excludes paths", async () => {
  const row = {
    session_id: S,
    workspace_id: W,
    worktree_id: null,
    additional_workspace_ids: [U, T],
  };
  const fetchImpl = reply({ roots: [{ ...row, path: "C:\\extra" }], truncated: false });
  expect(await createRootsApi("t", fetchImpl).roots()).toEqual({ roots: [row], truncated: false });
});

test("record posts only root IDs, including all seven extras", async () => {
  const row = {
    session_id: S,
    workspace_id: W,
    worktree_id: null,
    additional_workspace_ids: Object.freeze([...additional]),
  };
  const fetchImpl = reply({ root: { ...row, path: "C:\\extra" } }, 201);
  expect(
    await createRootsApi("t", fetchImpl).record({ ...row, path: "C:\\extra" } as typeof row),
  ).toEqual(row);
  expect(JSON.parse(call(fetchImpl)[1].body as string)).toEqual(row);
  expect(row.additional_workspace_ids).toEqual(additional);
});

test("empty additional lists keep the original root shape, including worktrees", async () => {
  for (const worktree_id of [null, T]) {
    const row = { session_id: S, workspace_id: W, worktree_id };
    const withEmpty = { ...row, additional_workspace_ids: [] };
    const fetchImpl = reply({ root: withEmpty }, 201);
    expect(await createRootsApi("t", fetchImpl).record(withEmpty)).toEqual(row);
    expect(JSON.parse(call(fetchImpl)[1].body as string)).toEqual(row);
    expect(
      await createRootsApi("t", reply({ roots: [withEmpty], truncated: false })).roots(),
    ).toEqual({ roots: [row], truncated: false });
  }
});

test.each([
  ["null", null, null],
  ["string", T, null],
  ["object", { id: T }, null],
  ["non-UUID", ["bad"], null],
  ["non-string", [1], null],
  ["sparse", Array<string>(1), null],
  ["uppercase UUID", [U.toUpperCase()], null],
  ["duplicate", [T, T], null],
  ["primary", [W], null],
  ["over cap", [...additional, U], null],
  ["worktree", [U], T],
])(
  "invalid additional list (%s) is refused before fetch and after response",
  async (_, extras, worktree_id) => {
    const row = {
      session_id: S,
      workspace_id: W,
      worktree_id,
      additional_workspace_ids: extras,
    };
    const fetchImpl = reply({});
    expect(await code(createRootsApi("t", fetchImpl).record(row as SessionRootJson))).toBe(
      "invalid_workspace",
    );
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(await code(createRootsApi("t", reply({ roots: [row], truncated: false })).roots())).toBe(
      "unavailable",
    );
    expect(
      await code(
        createRootsApi("t", reply({ root: row })).record({
          session_id: S,
          workspace_id: W,
          worktree_id: null,
        }),
      ),
    ).toBe("unavailable");
  },
);

test("worktrees parses rows and rejects a bad field", async () => {
  expect(
    await createRootsApi("t", reply({ worktrees: [tree()], truncated: true })).worktrees(),
  ).toEqual({
    worktrees: [tree()],
    truncated: true,
  });
  for (const bad of [
    { state: "gone" },
    { base_oid: "abc" },
    { worktree_id: "nope" },
    { branch: 1 },
    { path: null },
  ])
    expect(
      await code(
        createRootsApi("t", reply({ worktrees: [tree(bad)], truncated: false })).worktrees(),
      ),
    ).toBe("unavailable");
  expect(await code(createRootsApi("t", reply({ worktrees: [] })).worktrees())).toBe("unavailable");
});

const BOT_KINDS = ["session", "task", "thread"] as const;
const BODY = { policy_revision: 4, root_physical_id: "p" };

test.each(BOT_KINDS)("a %s bot_copy keeps exactly its four fields", async (kind) => {
  const bot_copy = { kind, bot_name: "<b>Ada</b>", title: "T\u2603", released: kind === "task" };
  const rows = [tree({ bot_copy: { ...bot_copy, path: "C:\\leak", extra: 1 } }), tree()];
  const answer = await createRootsApi(
    "t",
    reply({ worktrees: rows, truncated: false }),
  ).worktrees();
  expect(answer.worktrees[0]).toEqual(tree({ bot_copy }));
  expect(answer.worktrees[1]).toEqual(tree());
  expect("bot_copy" in (answer.worktrees[1] ?? {})).toBe(false);
});

test("a bot_copy keeps null names as unknown, not as a label", async () => {
  const bot_copy = { kind: "session", bot_name: null, title: null, released: true };
  const rows = [tree({ bot_copy })];
  expect(
    (await createRootsApi("t", reply({ worktrees: rows, truncated: false })).worktrees()).worktrees,
  ).toEqual(rows);
});

test.each([
  ["null", null],
  ["array", []],
  ["string", "Ada"],
  ["unknown kind", { kind: "main", bot_name: "A", title: "T", released: false }],
  ["missing kind", { bot_name: "A", title: "T", released: false }],
  ["number name", { kind: "session", bot_name: 1, title: "T", released: false }],
  ["missing name", { kind: "session", title: "T", released: false }],
  ["object title", { kind: "thread", bot_name: "A", title: {}, released: false }],
  ["missing title", { kind: "thread", bot_name: "A", released: false }],
  ["string released", { kind: "task", bot_name: "A", title: null, released: "yes" }],
  ["missing released", { kind: "task", bot_name: "A", title: null }],
])("a bad bot_copy (%s) makes the whole answer unavailable", async (_, bot_copy) => {
  const rows = [tree({ bot_copy })];
  expect(
    await code(createRootsApi("t", reply({ worktrees: rows, truncated: false })).worktrees()),
  ).toBe("unavailable");
  expect(await code(createRootsApi("t", reply({ worktree: rows[0] })).restore(T, BODY))).toBe(
    "unavailable",
  );
});

test("cleanup accepts the bot_chat reason", async () => {
  const kept = decision({ action: "keep", reason: "bot_chat" });
  expect(await createRootsApi("t", reply({ decisions: [kept] })).cleanup(W)).toEqual([kept]);
});

test("a 64-hex oid is accepted", async () => {
  const rows = [tree({ base_oid: "b".repeat(64) })];
  expect(
    (await createRootsApi("t", reply({ worktrees: rows, truncated: false })).worktrees()).worktrees,
  ).toEqual(rows);
});

test("create posts to the folder's route with the longer timeout", async () => {
  const fetchImpl = reply({ worktree: tree() }, 201);
  const body = { policy_revision: 3, root_physical_id: "p", branch: "viki/x" };
  expect(await createRootsApi("tok", fetchImpl).create(W, body)).toEqual(tree());
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/worktrees`);
  expect(init.method).toBe("POST");
  expect(JSON.parse(init.body as string)).toEqual(body);
});

test("remove parses the outcome", async () => {
  const kept = { worktree: tree({ state: "kept" }), outcome: "kept", reason: "dirty", changes: 2 };
  const fetchImpl = reply(kept);
  const body = { policy_revision: 3, root_physical_id: "p" };
  expect(await createRootsApi("tok", fetchImpl).remove(T, body)).toEqual(kept);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/worktrees/${T}/remove`);
  expect(JSON.parse(init.body as string)).toEqual(body);
  const done = {
    worktree: tree({ state: "removed" }),
    outcome: "removed",
    reason: null,
    changes: null,
  };
  expect(await createRootsApi("t", reply(done)).remove(T, body)).toEqual(done);
  for (const bad of [{ outcome: "x" }, { reason: "x" }, { changes: -1 }, { changes: 1.5 }])
    expect(await code(createRootsApi("t", reply({ ...kept, ...bad })).remove(T, body))).toBe(
      "unavailable",
    );
});

test("archives sends a Bearer GET and parses enabled plus rows", async () => {
  const fetchImpl = reply({ enabled: true, archives: [archive()] });
  expect(await createRootsApi("tok", fetchImpl).archives(W)).toEqual({
    enabled: true,
    archives: [archive()],
  });
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/worktree-archives`);
  expect(init.method).toBe("GET");
  expect(init.body).toBeUndefined();
  expect(await createRootsApi("t", reply({ enabled: false, archives: [] })).archives(W)).toEqual({
    enabled: false,
    archives: [],
  });
});

test("archives rejects a non-boolean enabled, a non-array list and a bad row", async () => {
  for (const body of [
    { enabled: "yes", archives: [] },
    { enabled: true, archives: "x" },
    { enabled: true, archives: [archive({ oid: "z" })] },
    { enabled: true, archives: [archive({ archived_at: 1 })] },
    { enabled: true, archives: [archive({ worktree_id: "nope" })] },
  ])
    expect(await code(createRootsApi("t", reply(body)).archives(W))).toBe("unavailable");
});

test("cleanup sends a Bearer GET and unwraps the decisions list", async () => {
  const fetchImpl = reply({ decisions: [decision()] });
  expect(await createRootsApi("tok", fetchImpl).cleanup(W)).toEqual([decision()]);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/worktree-cleanup`);
  expect(init.method).toBe("GET");
});

test("cleanup accepts null idle_since, eligible_at and bytes, and rejects a bad row", async () => {
  const nullable = decision({ idle_since: null, eligible_at: null, bytes: null });
  expect(await createRootsApi("t", reply({ decisions: [nullable] })).cleanup(W)).toEqual([
    nullable,
  ]);
  for (const bad of [
    decision({ action: "delete" }),
    decision({ reason: "nope" }),
    decision({ bytes: -1 }),
    decision({ bytes: 1.5 }),
    decision({ idle_since: 1 }),
    decision({ worktree_id: "nope" }),
  ])
    expect(await code(createRootsApi("t", reply({ decisions: [bad] })).cleanup(W))).toBe(
      "unavailable",
    );
  expect(await code(createRootsApi("t", reply({})).cleanup(W))).toBe("unavailable");
});

test("restore posts the policy fields to the worktree's route and parses the result", async () => {
  const fetchImpl = reply({ worktree: tree({ state: "ready" }) });
  const body = { policy_revision: 4, root_physical_id: "p" };
  expect(await createRootsApi("tok", fetchImpl).restore(T, body)).toEqual(tree({ state: "ready" }));
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/worktrees/${T}/restore`);
  expect(init.method).toBe("POST");
  expect(JSON.parse(init.body as string)).toEqual(body);
});

const SAVED = "ab".repeat(32);
const CURRENT = "cd".repeat(32);
const CONFIRM = { policy_revision: 4, root_physical_id: "p", expected_fingerprint: SAVED };

test("gitSettings sends a Bearer GET with no body and answers the saved fingerprint", async () => {
  const fetchImpl = reply({ fingerprint: SAVED });
  expect(await createRootsApi("tok", fetchImpl).gitSettings?.(W)).toBe(SAVED);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/git-settings`);
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual({ authorization: "Bearer tok" });
  expect(init.body).toBeUndefined();
  expect(await createRootsApi("t", reply({ fingerprint: null })).gitSettings?.(W)).toBeNull();
});

test.each([
  ["missing key", {}],
  ["uppercase", { fingerprint: SAVED.toUpperCase() }],
  ["short", { fingerprint: SAVED.slice(1) }],
  ["long", { fingerprint: `${SAVED}0` }],
  ["not hex", { fingerprint: "g".repeat(64) }],
  ["number", { fingerprint: 1 }],
  ["object", { fingerprint: { value: SAVED } }],
  ["empty", { fingerprint: "" }],
])("gitSettings refuses a %s fingerprint as unavailable", async (_, body) => {
  expect(await code(createRootsApi("t", reply(body)).gitSettings?.(W) as Promise<unknown>)).toBe(
    "unavailable",
  );
});

test("confirmGitSettings posts exactly three fields and answers the new fingerprint", async () => {
  const fetchImpl = reply({ fingerprint: CURRENT });
  const sent = { ...CONFIRM, extra: 1, fingerprint: CURRENT };
  expect(await createRootsApi("tok", fetchImpl).confirmGitSettings?.(W, sent)).toBe(CURRENT);
  const [url, init] = call(fetchImpl);
  expect(url).toBe(`/api/v2/code/${W}/git-settings`);
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({
    authorization: "Bearer tok",
    "content-type": "application/json",
  });
  expect(JSON.parse(init.body as string)).toEqual(CONFIRM);
});

test.each([
  ["null", { fingerprint: null }],
  ["missing", {}],
  ["uppercase", { fingerprint: CURRENT.toUpperCase() }],
  ["short", { fingerprint: CURRENT.slice(2) }],
  ["number", { fingerprint: 7 }],
])("confirmGitSettings refuses a %s answer as unavailable", async (_, body) => {
  expect(
    await code(
      createRootsApi("t", reply(body)).confirmGitSettings?.(W, CONFIRM) as Promise<unknown>,
    ),
  ).toBe("unavailable");
});

test("confirmGitSettings refuses a bad id or expected fingerprint before any fetch", async () => {
  const fetchImpl = reply({ fingerprint: CURRENT });
  const api = createRootsApi("t", fetchImpl);
  expect(await code(api.confirmGitSettings?.("bad", CONFIRM) as Promise<unknown>)).toBe(
    "invalid_workspace",
  );
  expect(await code(api.gitSettings?.("bad") as Promise<unknown>)).toBe("invalid_workspace");
  for (const expected_fingerprint of ["", SAVED.toUpperCase(), SAVED.slice(1), null as never])
    expect(
      await code(
        api.confirmGitSettings?.(W, { ...CONFIRM, expected_fingerprint }) as Promise<unknown>,
      ),
    ).toBe("invalid_body");
  expect(fetchImpl).not.toHaveBeenCalled();
});

test.each([
  ["git_settings_changed", 409],
  ["baseline_changed", 409],
  ["trust_required", 409],
  ["git_settings_unavailable", 503],
  ["unknown_workspace", 404],
])("the git-settings routes keep the server's %s code", async (errorCode, status) => {
  const answer = { error: { code: errorCode, message: "m" } };
  const api = createRootsApi("t", reply(answer, status));
  expect(await code(api.gitSettings?.(W) as Promise<unknown>)).toBe(errorCode);
  expect(await code(api.confirmGitSettings?.(W, CONFIRM) as Promise<unknown>)).toBe(errorCode);
});

test("the git-settings calls stop with the caller's signal", async () => {
  const seen: AbortSignal[] = [];
  const hang = vi.fn(
    (_url: unknown, init?: RequestInit) =>
      new Promise<Response>((_, reject) => {
        seen.push(init?.signal as AbortSignal);
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  ) as unknown as typeof fetch;
  const api = createRootsApi("t", hang);
  const read = new AbortController();
  const confirm = new AbortController();
  const reading = code(api.gitSettings?.(W, read.signal) as Promise<unknown>);
  const confirming = code(api.confirmGitSettings?.(W, CONFIRM, confirm.signal) as Promise<unknown>);
  expect(seen.map((signal) => signal.aborted)).toEqual([false, false]);
  read.abort();
  confirm.abort();
  expect(await reading).toBe("other");
  expect(await confirming).toBe("other");
  expect(seen.map((signal) => signal.aborted)).toEqual([true, true]);
  const already = new AbortController();
  already.abort();
  expect(await code(api.gitSettings?.(W, already.signal) as Promise<unknown>)).toBe("other");
  expect(hang).toHaveBeenCalledTimes(2);
});

test("error codes: the server's, else unavailable", async () => {
  const refused = (body: unknown, status: number) =>
    code(createRootsApi("t", reply(body, status)).roots());
  expect(await refused({ error: { code: "forbidden_origin", message: "m" } }, 403)).toBe(
    "forbidden_origin",
  );
  expect(await refused({ error: { message: "m" } }, 500)).toBe("unavailable");
  expect(await refused({}, 500)).toBe("unavailable");
  const garbage = vi.fn(async () => new Response("<html>", { status: 502 })) as typeof fetch;
  expect(await code(createRootsApi("t", garbage).roots())).toBe("unavailable");
});

test("a non-UUID id fails before any fetch", async () => {
  const fetchImpl = reply({});
  const api = createRootsApi("t", fetchImpl);
  const body = { policy_revision: 1, root_physical_id: "p" };
  expect(await code(api.create("bad", { ...body, branch: "b" }))).toBe("invalid_workspace");
  expect(await code(api.remove("bad", body))).toBe("invalid_workspace");
  expect(await code(api.archives("bad"))).toBe("invalid_workspace");
  expect(await code(api.cleanup("bad"))).toBe("invalid_workspace");
  expect(await code(api.restore("bad", body))).toBe("invalid_workspace");
  expect(await code(api.record({ session_id: "bad", workspace_id: W, worktree_id: null }))).toBe(
    "invalid_workspace",
  );
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("the serialized 4096-row root list with seven extras fits the roots-only 2 MiB budget", async () => {
  const rows = Array.from({ length: 4096 }, (_, index) => ({
    session_id: `${index.toString(16).padStart(8, "0")}-1111-4111-8111-111111111111`,
    workspace_id: W,
    worktree_id: null,
    additional_workspace_ids: additional,
  }));
  const body = JSON.stringify({ roots: rows, truncated: false });
  const bytes = new TextEncoder().encode(body).byteLength;
  expect(bytes).toBe(1_757_213);
  expect(bytes).toBeGreaterThan(1024 * 1024);
  expect(bytes).toBeLessThan(2 * 1024 * 1024);
  const fetchImpl = vi.fn(async () => new Response(body)) as typeof fetch;
  expect(await createRootsApi("t", fetchImpl).roots()).toEqual({ roots: rows, truncated: false });
});

test("oversized roots are rejected before JSON parsing", async () => {
  const body = JSON.stringify({
    roots: [],
    truncated: false,
    padding: "x".repeat(2 * 1024 * 1024),
  });
  const big = vi.fn(async () => new Response(body)) as typeof fetch;
  const parse = vi.spyOn(JSON, "parse");
  try {
    expect(await code(createRootsApi("t", big).roots())).toBe("unavailable");
    expect(parse).not.toHaveBeenCalled();
  } finally {
    parse.mockRestore();
  }
});

test("worktree reads and root POST answers keep the 1 MiB budget before parsing", async () => {
  const row = { session_id: S, workspace_id: W, worktree_id: null };
  const body = JSON.stringify({
    root: row,
    worktrees: [tree()],
    truncated: false,
    padding: "x".repeat(1024 * 1024),
  });
  const big = vi.fn(async () => new Response(body)) as typeof fetch;
  const parse = vi.spyOn(JSON, "parse");
  try {
    expect(await code(createRootsApi("t", big).worktrees())).toBe("unavailable");
    expect(await code(createRootsApi("t", big).record(row))).toBe("unavailable");
    expect(parse).not.toHaveBeenCalled();
  } finally {
    parse.mockRestore();
  }
});
