import { expect, test, vi } from "vitest";
import { CodeApiError, createCodeApi } from "./codeApi";

const ID = "11111111-1111-4111-8111-111111111111";
const SHA = "a".repeat(64);

function reply(body: unknown, status = 200): typeof fetch {
  return vi.fn(async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
}
const refusal = (promise: Promise<unknown>) =>
  promise.then(
    () => null,
    (error: unknown) => (error instanceof CodeApiError ? error.code : "other"),
  );

test("parses a tree body and sends a Bearer GET with an encoded query", async () => {
  const body = {
    path: "src",
    entries: [
      { name: "a.py", type: "file", size: 12 },
      { name: "pkg", type: "dir", size: null },
    ],
    truncated: false,
  };
  const fetchImpl = reply(body);
  expect(await createCodeApi("tok", fetchImpl).tree(ID, "src/a b")).toEqual(body);
  const [url, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
  expect(url).toBe(`/api/v2/code/${ID}/tree?path=src%2Fa%20b`);
  expect(init.method).toBe("GET");
  expect(init.headers).toEqual({ authorization: "Bearer tok" });
});

test("parses a file body", async () => {
  const body = {
    path: "a.py",
    size: 3,
    sha256: SHA,
    encoding: "utf-8",
    newline: "lf",
    text: "x\n",
    reason: null,
  };
  expect(await createCodeApi("t", reply(body)).file(ID, "a.py")).toEqual(body);
  const binary = { ...body, encoding: null, newline: null, text: null, reason: "binary" };
  expect(await createCodeApi("t", reply(binary)).file(ID, "a.bin")).toEqual(binary);
});

test("parses a search body", async () => {
  const body = { hits: ["a.py", "b/c.py"], truncated: true };
  expect(await createCodeApi("t", reply(body)).search(ID, "a&b")).toEqual(body);
});

test("parses a changes body", async () => {
  const body = {
    state: "ready",
    branch: "main",
    head: "0123456789ab",
    detached: false,
    upstream: "origin/main",
    ahead: 1,
    behind: 0,
    entries: [
      { path: "a.py", orig_path: null, index: "M", worktree: ".", kind: "tracked", private: false },
      {
        path: "n.py",
        orig_path: "o.py",
        index: "R",
        worktree: "?",
        kind: "untracked",
        private: true,
      },
    ],
    truncated: false,
  };
  expect(await createCodeApi("t", reply(body)).changes(ID)).toEqual(body);
  const unknown = { ...body, state: "unknown", branch: null, head: null, entries: [] };
  expect((await createCodeApi("t", reply(unknown)).changes(ID)).state).toBe("unknown");
});

test("parses a diff body", async () => {
  const body = { path: "a.py", text: "@@ -1 +1 @@\n", reason: null };
  expect(await createCodeApi("t", reply(body)).diff(ID, "a.py")).toEqual(body);
  const none = { path: "a.py", text: null, reason: "no_diff" };
  expect(await createCodeApi("t", reply(none)).diff(ID, "a.py")).toEqual(none);
});

test("a bad shape is unavailable", async () => {
  const badTree = { path: "", entries: [{ name: "a", type: "socket", size: 1 }], truncated: false };
  expect(await refusal(createCodeApi("t", reply(badTree)).tree(ID, ""))).toBe("unavailable");
  const badChange = {
    state: "ready",
    branch: null,
    head: "XYZ",
    detached: false,
    upstream: null,
    ahead: null,
    behind: null,
    entries: [],
    truncated: false,
  };
  expect(await refusal(createCodeApi("t", reply(badChange)).changes(ID))).toBe("unavailable");
  const entry = { path: "a", orig_path: null, index: "M", worktree: ".", kind: "tracked" };
  for (const bad of [{ index: "X" }, { worktree: "X" }, { kind: "ignored" }, { private: "yes" }]) {
    const entries = [{ ...entry, private: false, ...bad }];
    const body = { ...badChange, head: null, entries };
    expect(await refusal(createCodeApi("t", reply(body)).changes(ID))).toBe("unavailable");
  }
  expect(
    await refusal(createCodeApi("t", reply({ hits: [1], truncated: false })).search(ID, "x")),
  ).toBe("unavailable");
});

test("an error body maps to its code; a bare failure is unavailable", async () => {
  const body = { error: { code: "trust_required", message: "m" } };
  expect(await refusal(createCodeApi("t", reply(body, 409)).changes(ID))).toBe("trust_required");
  expect(await refusal(createCodeApi("t", reply({}, 500)).changes(ID))).toBe("unavailable");
});

const OP = "22222222-2222-4222-8222-222222222222";
const SAVE = {
  policy_revision: 3,
  root_physical_id: "root-1",
  content: "x\n",
  expected_sha256: SHA,
  encoding: "utf-8",
  newline: "lf",
} as const;

test("save sends a Bearer JSON PUT and parses saved and no_change", async () => {
  const saved = { operation_id: OP, status: "saved", sha256: SHA, cleanup_pending: false };
  const fetchImpl = reply(saved);
  expect(await createCodeApi("tok", fetchImpl).save(ID, "src/a b.py", SAVE)).toEqual(saved);
  const [url, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
  expect(url).toBe(`/api/v2/code/${ID}/file?path=src%2Fa%20b.py`);
  expect(init.method).toBe("PUT");
  expect(init.headers).toEqual({
    authorization: "Bearer tok",
    "content-type": "application/json",
  });
  expect(JSON.parse(init.body as string)).toEqual(SAVE);
  const same = { ...saved, status: "no_change", cleanup_pending: true };
  expect(await createCodeApi("t", reply(same)).save(ID, "a.py", SAVE)).toEqual(same);
});

test("save parses a conflict", async () => {
  const conflict = { operation_id: OP, status: "conflict", sha256: null, cleanup_pending: false };
  expect(await createCodeApi("t", reply(conflict, 409)).save(ID, "a.py", SAVE)).toEqual(conflict);
});

test("save throws a refusal's code and carries its operation id", async () => {
  const trust = { error: { code: "trust_required", message: "m" } };
  expect(await refusal(createCodeApi("t", reply(trust, 409)).save(ID, "a.py", SAVE))).toBe(
    "trust_required",
  );
  const recovery = { error: { code: "recovery_required", message: "m", operation_id: OP } };
  const error = await createCodeApi("t", reply(recovery, 503))
    .save(ID, "a.py", SAVE)
    .catch((e: unknown) => e);
  expect(error).toBeInstanceOf(CodeApiError);
  expect((error as CodeApiError).code).toBe("recovery_required");
  expect((error as CodeApiError).operationId).toBe(OP);
  expect(new CodeApiError("x").operationId).toBeNull();
  expect(await refusal(createCodeApi("t", reply({}, 500)).save(ID, "a.py", SAVE))).toBe(
    "unavailable",
  );
});

test("save treats a malformed 200 or a non-conflict 409 as unavailable", async () => {
  const ok = { operation_id: OP, status: "saved", sha256: SHA, cleanup_pending: false };
  const bads = [
    { ...ok, status: "conflict" },
    { ...ok, status: "other" },
    { ...ok, sha256: null },
    { ...ok, sha256: "abc" },
    { ...ok, operation_id: "nope" },
    { ...ok, cleanup_pending: "no" },
  ];
  for (const bad of bads)
    expect(await refusal(createCodeApi("t", reply(bad)).save(ID, "a.py", SAVE))).toBe(
      "unavailable",
    );
  const notConflict = { ...ok, status: "saved" };
  expect(await refusal(createCodeApi("t", reply(notConflict, 409)).save(ID, "a.py", SAVE))).toBe(
    "unavailable",
  );
});

test("save refuses a bad workspace or a lone surrogate before any request", async () => {
  const fetchImpl = reply({});
  const api = createCodeApi("t", fetchImpl);
  expect(await refusal(api.save("nope", "a.py", SAVE))).toBe("invalid_workspace");
  expect(await refusal(api.save(ID, "a.py", { ...SAVE, content: "a\ud800b" }))).toBe(
    "invalid_content",
  );
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("a bad workspace id is refused before any request", async () => {
  const fetchImpl = reply({});
  expect(await refusal(createCodeApi("t", fetchImpl).changes("nope"))).toBe("invalid_workspace");
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("a response over the route's cap is unavailable", async () => {
  const body = { hits: ["x".repeat(300 * 1024)], truncated: false };
  expect(await refusal(createCodeApi("t", reply(body)).search(ID, "x"))).toBe("unavailable");
});

test("a caller abort cancels the request", async () => {
  const fetchImpl = vi.fn(
    (_url: unknown, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  ) as typeof fetch;
  const controller = new AbortController();
  const pending = refusal(createCodeApi("t", fetchImpl).changes(ID, controller.signal));
  controller.abort();
  expect(await pending).toBe("other"); // the fetch rejection passes through, as in vaultOpsApi
  const done = new AbortController();
  done.abort();
  await expect(createCodeApi("t", fetchImpl).changes(ID, done.signal)).rejects.toThrow("cancelled");
});
