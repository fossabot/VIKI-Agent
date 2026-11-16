import { afterEach, expect, test, vi } from "vitest";
import {
  type ApprovalInput,
  type CommandInput,
  createDurableApi,
  DurableApiError,
} from "./durableApi";

const SESSION = "11111111-1111-1111-1111-111111111111";
const GENERATION = "22222222-2222-2222-2222-222222222222";
const COMMAND = "33333333-3333-3333-3333-333333333333";
const OTHER = "44444444-4444-4444-4444-444444444444";
const input: CommandInput = {
  command_id: COMMAND,
  session_id: SESSION,
  generation: GENERATION,
  draft_revision: 2,
};
const draft = { key: SESSION, revision: 2, text: "Edited", saved_at: "2026-09-14T00:00:00.000Z" };
const receipt = {
  ...input,
  state: "accepted",
  reason: "",
  created_at: "2026-09-14T00:00:00.000Z",
  updated_at: "2026-09-14T00:00:00.000Z",
  bytes: 6,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function fakeFetch(handler: (url: RequestInfo | URL, init?: RequestInit) => Promise<Response>) {
  return vi.fn(handler);
}

afterEach(() => {
  vi.useRealTimers();
});

test("draft GET and revision-checked clear use bearer auth and the exact wire shape", async () => {
  const fetchImpl = fakeFetch(async (_url, init) =>
    json(init?.method === "PUT" ? { ...draft, revision: 3, text: "" } : draft),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect(await api.getDraft(SESSION)).toEqual(draft);
  expect(await api.clearDraft(SESSION, 2)).toEqual({ ...draft, revision: 3, text: "" });
  expect(fetchImpl).toHaveBeenCalledTimes(2);
  expect(fetchImpl.mock.calls[0]?.[0]).toBe(`/api/v2/drafts/${SESSION}`);
  expect(fetchImpl.mock.calls[0]?.[1]).toMatchObject({
    method: "GET",
    headers: { authorization: "Bearer secret" },
  });
  expect(fetchImpl.mock.calls[1]?.[1]).toMatchObject({
    method: "PUT",
    headers: { authorization: "Bearer secret", "content-type": "application/json" },
    body: JSON.stringify({ expected_revision: 2, text: "" }),
  });
  expect(String(fetchImpl.mock.calls[1]?.[0])).not.toContain("secret");
});

test("409 draft conflicts retain the server revision but never expose its message", async () => {
  const fetchImpl = fakeFetch(async () =>
    json(
      {
        error: { code: "conflict", message: "private competing text", current_revision: 9 },
      },
      409,
    ),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.saveDraft(SESSION, 2, "Edited")).rejects.toMatchObject({
    status: 409,
    code: "conflict",
    currentRevision: 9,
  });
  await expect(api.saveDraft(SESSION, 2, "Edited")).rejects.not.toThrow("private competing text");
  expect(fetchImpl).toHaveBeenCalledTimes(2); // Each explicit call has one request.
});

test("unknown server errors and malformed success bodies have safe fixed errors", async () => {
  const fetchImpl = fakeFetch(async (_url, init) =>
    init?.method === "PUT"
      ? json({ ...draft, key: OTHER, text: "changed" })
      : json({ error: { code: "surprise_private_error", message: "secret text" } }, 503),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.getDraft(SESSION)).rejects.toMatchObject({ status: 503, code: "http_error" });
  await expect(api.saveDraft(SESSION, 2, "Edited")).rejects.toMatchObject({
    status: 0,
    code: "invalid_response",
  });
});

test("a finite timeout aborts even a fetch mock that never settles", async () => {
  vi.useFakeTimers();
  let signal: AbortSignal | undefined;
  const fetchImpl = fakeFetch(async (_url, init) => {
    signal = init?.signal ?? undefined;
    return new Promise<Response>(() => {});
  });
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch, timeoutMs: 20 });
  const pending = api.getDraft(SESSION);
  const result = expect(pending).rejects.toMatchObject({ status: 0, code: "timeout" });
  await vi.advanceTimersByTimeAsync(20);
  await result;
  expect(signal?.aborted).toBe(true);
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

test("network failure and missing command differ for ambiguous submission recovery", async () => {
  const fetchImpl = fakeFetch(async (_url, init) => {
    if (init?.method === "POST") throw new Error("private network detail");
    return json({ error: { code: "not_found", message: "missing" } }, 404);
  });
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.submitCommand(input)).rejects.toMatchObject({
    status: 0,
    code: "network_error",
  });
  await expect(api.getCommand(COMMAND)).rejects.toMatchObject({ status: 404, code: "not_found" });
  expect(fetchImpl).toHaveBeenCalledTimes(2);
});

test("submission accepts 202, checks the entire immutable tuple, and never retries", async () => {
  const fetchImpl = fakeFetch(async () => json(receipt, 202));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect(await api.submitCommand(input)).toEqual(receipt);
  expect(fetchImpl.mock.calls[0]?.[0]).toBe("/api/v2/commands");
  expect(fetchImpl.mock.calls[0]?.[1]?.body).toBe(JSON.stringify(input));
  expect(fetchImpl).toHaveBeenCalledTimes(1);

  fetchImpl.mockImplementation(async () => json({ ...receipt, generation: OTHER }, 200));
  await expect(api.submitCommand(input)).rejects.toMatchObject({
    status: 0,
    code: "invalid_response",
  });
  expect(fetchImpl).toHaveBeenCalledTimes(2);
});

test("target, ordered metadata pages, and exact submitted text are validated", async () => {
  const fetchImpl = fakeFetch(async (url) => {
    const path = String(url);
    if (path.endsWith("/target")) {
      return json({
        session_id: SESSION,
        generation: GENERATION,
        closed: false,
        busy: false,
        live: true,
      });
    }
    if (path.includes("/submission")) return json({ ...receipt, text: "Edited" });
    if (path.startsWith("/api/v2/drafts?")) {
      return json({
        drafts: [{ key: SESSION, revision: 2, saved_at: draft.saved_at, bytes: 6 }],
        next_after: null,
      });
    }
    return json({ commands: [receipt], next_after: null });
  });
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect((await api.getTarget(SESSION)).generation).toBe(GENERATION);
  expect((await api.listDrafts({ limit: 1 })).drafts[0]?.key).toBe(SESSION);
  expect((await api.listCommands({ limit: 1 })).commands[0]?.command_id).toBe(COMMAND);
  expect((await api.getSubmission(COMMAND)).text).toBe("Edited");
  expect(fetchImpl.mock.calls.map(([url]) => String(url))).toEqual([
    `/api/v2/sessions/${SESSION}/target`,
    "/api/v2/drafts?limit=1",
    "/api/v2/commands?limit=1",
    `/api/v2/commands/${COMMAND}/submission`,
  ]);
});

test("invalid page and response identities fail without another request", async () => {
  const fetchImpl = fakeFetch(async () =>
    json({ commands: [receipt, receipt], next_after: COMMAND }),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.listCommands({ limit: 101 })).rejects.toBeInstanceOf(DurableApiError);
  expect(fetchImpl).not.toHaveBeenCalled();
  await expect(api.listCommands({ limit: 2 })).rejects.toMatchObject({ code: "invalid_response" });
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

test("metadata reads refuse accidentally included submitted or draft text", async () => {
  const fetchImpl = fakeFetch(async (url) =>
    String(url).includes("drafts")
      ? json({
          drafts: [
            { key: SESSION, revision: 2, saved_at: draft.saved_at, bytes: 6, text: "Edited" },
          ],
          next_after: null,
        })
      : json({ ...receipt, text: "Edited" }),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.listDrafts()).rejects.toMatchObject({ code: "invalid_response" });
  await expect(api.getCommand(COMMAND)).rejects.toMatchObject({ code: "invalid_response" });
});

test("exact draft-revision lookup returns metadata or null only for an authenticated 404", async () => {
  const fetchImpl = fakeFetch(async (url) =>
    String(url).endsWith("revision=3")
      ? json({ error: { code: "not_found", message: "missing" } }, 404)
      : json(receipt),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect(await api.getDraftCommand(SESSION, 2)).toEqual(receipt);
  expect(await api.getDraftCommand(SESSION, 3)).toBeNull();
  expect(fetchImpl.mock.calls.map(([url]) => String(url))).toEqual([
    `/api/v2/sessions/${SESSION}/draft-command?revision=2`,
    `/api/v2/sessions/${SESSION}/draft-command?revision=3`,
  ]);
  expect(fetchImpl.mock.calls[0]?.[1]?.headers).toEqual({ authorization: "Bearer secret" });
});

test("draft-revision lookup refuses a mismatched receipt and preserves network uncertainty", async () => {
  const fetchImpl = fakeFetch(async () => json({ ...receipt, draft_revision: 3 }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.getDraftCommand(SESSION, 2)).rejects.toMatchObject({
    status: 0,
    code: "invalid_response",
  });
  fetchImpl.mockImplementation(async () => {
    throw new Error("private network error");
  });
  await expect(api.getDraftCommand(SESSION, 2)).rejects.toMatchObject({
    status: 0,
    code: "network_error",
  });
  await expect(api.getDraftCommand(SESSION, 0)).rejects.toMatchObject({ code: "invalid_input" });
  expect(fetchImpl).toHaveBeenCalledTimes(2);
});

test("a second command ID for one saved revision preserves the atomic 409 code", async () => {
  const fetchImpl = fakeFetch(async () =>
    json({ error: { code: "draft_already_submitted", message: "private detail" } }, 409),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.submitCommand(input)).rejects.toMatchObject({
    status: 409,
    code: "draft_already_submitted",
  });
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

test("a running-turn refusal keeps its own code, unlike an unlisted 409", async () => {
  // turn_running is an actor admission refusal, not the storage busy code. The safelist
  // must admit it by name so editors.ts can tell a refused send from a failed save.
  const fetchImpl = fakeFetch(async () =>
    json({ error: { code: "turn_running", message: "private detail" } }, 409),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.submitCommand(input)).rejects.toMatchObject({
    status: 409,
    code: "turn_running",
  });
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

test("submission and draft attachment refusals keep their own codes", async () => {
  // unsupported_attachment/invalid_attachment (400) and attachment_unavailable (409) are
  // the command and draft services' own refusal codes, never the generic http_error fallback.
  const fetchImpl = fakeFetch(async () =>
    json({ error: { code: "unsupported_attachment", message: "private detail" } }, 400),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.submitCommand(input)).rejects.toMatchObject({
    status: 400,
    code: "unsupported_attachment",
  });
  fetchImpl.mockImplementation(async () =>
    json({ error: { code: "invalid_attachment", message: "private detail" } }, 400),
  );
  await expect(api.saveDraft(SESSION, 2, "Edited")).rejects.toMatchObject({
    status: 400,
    code: "invalid_attachment",
  });
  fetchImpl.mockImplementation(async () =>
    json({ error: { code: "attachment_unavailable", message: "private detail" } }, 409),
  );
  await expect(api.submitCommand(input)).rejects.toMatchObject({
    status: 409,
    code: "attachment_unavailable",
  });
  await expect(api.saveDraft(SESSION, 2, "Edited")).rejects.toMatchObject({
    status: 409,
    code: "attachment_unavailable",
  });
  expect(fetchImpl).toHaveBeenCalledTimes(4);
});

test("a not_dispatched receipt validates the two attachment reasons", async () => {
  // COMMAND_REASONS must admit these so a receipt read (getCommand/listCommands) for a
  // dispatch-time attachment failure is not thrown away as invalid_response.
  const fetchImpl = fakeFetch(async () =>
    json({ ...receipt, state: "not_dispatched", reason: "unsupported_attachment" }),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect((await api.getCommand(COMMAND)).reason).toBe("unsupported_attachment");
  fetchImpl.mockImplementation(async () =>
    json({ ...receipt, state: "not_dispatched", reason: "attachment_unavailable" }),
  );
  expect((await api.getCommand(COMMAND)).reason).toBe("attachment_unavailable");
});

test.each([
  "excerpt_with_command",
  "excerpt_wrong_vault",
  "excerpt_stale",
  "excerpt_unavailable",
  "excerpts_too_large",
])("a not_dispatched receipt keeps the excerpt reason %s", async (reason) => {
  // The server's REASONS admits these; a receipt read must not reject what it recorded.
  const fetchImpl = fakeFetch(async () => json({ ...receipt, state: "not_dispatched", reason }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect((await api.getCommand(COMMAND)).reason).toBe(reason);
});

/* The approval ledger's wire. The client answers only through these routes, so what
   it refuses to believe about a response matters as much as what it sends. */

const RUNTIME = "55555555-5555-5555-5555-555555555555";
const REQUEST = "66666666-6666-6666-6666-666666666666";
const TURN = "77777777-7777-7777-7777-777777777777";
const CHOICE = "88888888-8888-8888-8888-888888888888";

const approvalInput: ApprovalInput = {
  choice_id: CHOICE,
  session_id: SESSION,
  generation: GENERATION,
  tool_use_id: "toolu_1",
  native_request: { runtime_id: RUNTIME, request_id: REQUEST, turn_id: TURN },
  choice: { kind: "permission", allow: true },
};

const approvalReceipt = {
  choice_id: CHOICE,
  session_id: SESSION,
  generation: GENERATION,
  runtime_id: RUNTIME,
  request_id: REQUEST,
  turn_id: TURN,
  tool_use_id: "toolu_1",
  state: "delivered_locally",
  reason: "",
  created_at: "2026-09-16T00:00:00.000Z",
  updated_at: "2026-09-16T00:00:00.000Z",
};

test("recording a choice sends the exact body once, keeps the token out of the URL, and takes 202 or 200", async () => {
  const fetchImpl = fakeFetch(async () => json(approvalReceipt, 202));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect(await api.recordApproval(approvalInput)).toEqual(approvalReceipt);
  expect(fetchImpl.mock.calls[0]?.[0]).toBe("/api/v2/approvals");
  expect(fetchImpl.mock.calls[0]?.[1]).toMatchObject({
    method: "POST",
    headers: { authorization: "Bearer secret", "content-type": "application/json" },
    body: JSON.stringify({
      choice_id: CHOICE,
      session_id: SESSION,
      generation: GENERATION,
      tool_use_id: "toolu_1",
      native_request: { runtime_id: RUNTIME, request_id: REQUEST, turn_id: TURN },
      choice: { kind: "permission", allow: true },
    }),
  });
  expect(String(fetchImpl.mock.calls[0]?.[0])).not.toContain("secret");
  expect(fetchImpl).toHaveBeenCalledTimes(1);

  // The replay of an identical POST answers 200 with the same receipt, and is not a new attempt.
  fetchImpl.mockImplementation(async () => json(approvalReceipt, 200));
  expect(await api.recordApproval(approvalInput)).toEqual(approvalReceipt);
  expect(fetchImpl).toHaveBeenCalledTimes(2);
});

test("a receipt for another request, or one carrying the choice, is refused", async () => {
  const fetchImpl = fakeFetch(async () =>
    json({ ...approvalReceipt, tool_use_id: "toolu_2" }, 202),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.recordApproval(approvalInput)).rejects.toMatchObject({
    status: 0,
    code: "invalid_response",
  });
  // Metadata routes never carry the user's answer: a body that does is not this contract's.
  fetchImpl.mockImplementation(async () =>
    json({ ...approvalReceipt, choice: { kind: "permission", allow: true } }),
  );
  await expect(api.getApproval(CHOICE)).rejects.toMatchObject({ code: "invalid_response" });
});

test("a state and reason that cannot both be true are refused", async () => {
  const fetchImpl = fakeFetch(async () => json({ ...approvalReceipt, reason: "storage" }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  // delivered_locally never carries a reason...
  await expect(api.getApproval(CHOICE)).rejects.toMatchObject({ code: "invalid_response" });
  // ...not_delivered always does...
  fetchImpl.mockImplementation(async () =>
    json({ ...approvalReceipt, state: "not_delivered", reason: "" }),
  );
  await expect(api.getApproval(CHOICE)).rejects.toMatchObject({ code: "invalid_response" });
  // ...and a reason outside the ledger's own set is not a reason.
  fetchImpl.mockImplementation(async () =>
    json({ ...approvalReceipt, state: "unknown", reason: "because" }),
  );
  await expect(api.getApproval(CHOICE)).rejects.toMatchObject({ code: "invalid_response" });
});

test("the 409s a second choice can meet keep their own codes", async () => {
  const fetchImpl = fakeFetch(async () =>
    json({ error: { code: "already_decided", message: "private detail" } }, 409),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.recordApproval(approvalInput)).rejects.toMatchObject({
    status: 409,
    code: "already_decided",
  });
  fetchImpl.mockImplementation(async () => json({ error: { code: "stale_request" } }, 409));
  await expect(api.recordApproval(approvalInput)).rejects.toMatchObject({
    status: 409,
    code: "stale_request",
  });
  fetchImpl.mockImplementation(async () => json({ error: { code: "invalid_choice" } }, 400));
  await expect(api.recordApproval(approvalInput)).rejects.toMatchObject({
    status: 400,
    code: "invalid_choice",
  });
  expect(fetchImpl).toHaveBeenCalledTimes(3);
});

test("the per-request lookup uses exactly the two query keys and reads null only from a 404", async () => {
  const fetchImpl = fakeFetch(async (url) =>
    String(url).includes(REQUEST)
      ? json(approvalReceipt)
      : json({ error: { code: "not_found", message: "none" } }, 404),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect(await api.findApproval(SESSION, RUNTIME, REQUEST)).toEqual(approvalReceipt);
  expect(await api.findApproval(SESSION, RUNTIME, OTHER)).toBeNull();
  expect(fetchImpl.mock.calls.map(([url]) => String(url))).toEqual([
    `/api/v2/sessions/${SESSION}/approval?runtime_id=${RUNTIME}&request_id=${REQUEST}`,
    `/api/v2/sessions/${SESSION}/approval?runtime_id=${RUNTIME}&request_id=${OTHER}`,
  ]);
  expect(fetchImpl.mock.calls[0]?.[1]?.headers).toEqual({ authorization: "Bearer secret" });
  // A 503 is never an answer about whether a record exists.
  fetchImpl.mockImplementation(async () => json({ error: { code: "unavailable" } }, 503));
  await expect(api.findApproval(SESSION, RUNTIME, REQUEST)).rejects.toMatchObject({
    status: 503,
    code: "unavailable",
  });
});

test("the choice route returns the recorded answer and the displayed request, and refuses neither missing", async () => {
  const record = {
    ...approvalReceipt,
    choice: { kind: "question", answers: { "Where should it go?": "30-knowledge" }, response: "" },
    request: { type: "question.asked", data: { tool_use_id: "toolu_1", questions: [] } },
  };
  const fetchImpl = fakeFetch(async () => json(record));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  const got = await api.getApprovalChoice(CHOICE);
  expect(got.choice).toEqual(record.choice);
  expect(got.request.type).toBe("question.asked");
  expect(fetchImpl.mock.calls[0]?.[0]).toBe(`/api/v2/approvals/${CHOICE}/choice`);

  fetchImpl.mockImplementation(async () => json({ ...approvalReceipt, choice: record.choice }));
  await expect(api.getApprovalChoice(CHOICE)).rejects.toMatchObject({ code: "invalid_response" });
  fetchImpl.mockImplementation(async () =>
    json({ ...approvalReceipt, request: record.request, choice: { kind: "permission" } }),
  );
  await expect(api.getApprovalChoice(CHOICE)).rejects.toMatchObject({ code: "invalid_response" });
});

test("an approval page is ordered, bounded and validated before anything is sent", async () => {
  const fetchImpl = fakeFetch(async () => json({ approvals: [approvalReceipt], next_after: null }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect((await api.listApprovals({ limit: 1 })).approvals[0]?.choice_id).toBe(CHOICE);
  expect(fetchImpl.mock.calls[0]?.[0]).toBe("/api/v2/approvals?limit=1");
  await expect(api.listApprovals({ limit: 0 })).rejects.toBeInstanceOf(DurableApiError);
  expect(fetchImpl).toHaveBeenCalledTimes(1);
  fetchImpl.mockImplementation(async () =>
    json({ approvals: [approvalReceipt, approvalReceipt], next_after: null }),
  );
  await expect(api.listApprovals({ limit: 2 })).rejects.toMatchObject({ code: "invalid_response" });
});

test("a card with no tool-use id of its own is still answerable, and readable back", async () => {
  // driver.py builds such a card with `ctx.tool_use_id or ""`, the DDL admits it and the service
  // accepts it; a client that refused it would leave that one card permanently unanswerable.
  const fetchImpl = fakeFetch(async (_url, init) =>
    json({ ...approvalReceipt, tool_use_id: "" }, init?.method === "POST" ? 202 : 200),
  );
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  const receipt = await api.recordApproval({ ...approvalInput, tool_use_id: "" });
  expect(receipt.tool_use_id).toBe("");
  expect(JSON.parse(String(fetchImpl.mock.calls[0]?.[1]?.body)).tool_use_id).toBe("");
  // ...and a record found for it reads back rather than failing validation.
  expect((await api.findApproval(SESSION, RUNTIME, REQUEST))?.tool_use_id).toBe("");
  // The ceiling still holds.
  await expect(
    api.recordApproval({ ...approvalInput, tool_use_id: "x".repeat(257) }),
  ).rejects.toMatchObject({ code: "invalid_input" });
  expect(fetchImpl).toHaveBeenCalledTimes(2);
});

test("a malformed choice or identifier never leaves the browser", async () => {
  const fetchImpl = fakeFetch(async () => json(approvalReceipt, 202));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(
    api.recordApproval({
      ...approvalInput,
      choice: {
        kind: "question",
        answers: { a: 1 } as unknown as Record<string, string>,
        response: "",
      },
    }),
  ).rejects.toMatchObject({ code: "invalid_input" });
  await expect(api.getApproval("not-a-uuid")).rejects.toMatchObject({ code: "invalid_input" });
  await expect(api.findApproval(SESSION, RUNTIME, "nope")).rejects.toMatchObject({
    code: "invalid_input",
  });
  expect(fetchImpl).not.toHaveBeenCalled();
});

const VAULT = "66666666-6666-4666-8666-666666666666";
const PART = {
  kind: "vault_excerpt",
  vault_id: VAULT,
  ref: "30-knowledge/a.md",
  start_line: 2,
  end_line: 4,
  sha256: "e".repeat(64),
} as const;

test("a submitted turn exposes validated excerpts while legacy text-only records stay readable", async () => {
  const fetchImpl = fakeFetch(async () => json({ ...receipt, text: "Edited", parts: [PART] }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect((await api.getSubmission(COMMAND)).parts).toEqual([PART]);
  expect(fetchImpl.mock.calls[0]?.[0]).toBe(`/api/v2/commands/${COMMAND}/submission`);
  fetchImpl.mockImplementation(async () => json({ ...receipt, text: "Edited" }));
  expect((await api.getSubmission(COMMAND)).parts).toBeUndefined();
  fetchImpl.mockImplementation(async () => json({ ...receipt, text: "Edited", parts: [] }));
  expect((await api.getSubmission(COMMAND)).parts).toEqual([]);
  expect(fetchImpl).toHaveBeenCalledTimes(3);
});

test.each([
  ["not an array", {}],
  ["too many parts", Array.from({ length: 9 }, () => PART)],
  ["wrong kind", [{ ...PART, kind: "other" }]],
  ["wrong vault ID", [{ ...PART, vault_id: "other" }]],
  ["empty ref", [{ ...PART, ref: "" }]],
  ["invalid hash", [{ ...PART, sha256: "invalid" }]],
  ["reversed range", [{ ...PART, start_line: 5 }]],
  ["unbounded line", [{ ...PART, end_line: 10_000_000 }]],
])("a submitted turn rejects malformed excerpt parts: %s", async (_name, parts) => {
  const fetchImpl = fakeFetch(async () => json({ ...receipt, text: "Edited", parts }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.getSubmission(COMMAND)).rejects.toMatchObject({
    status: 0,
    code: "invalid_response",
  });
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

test("a draft save carrying an excerpt sends the parts list and round-trips the echo", async () => {
  const saved = { ...draft, revision: 3, text: "With a quote", parts: [PART] };
  const fetchImpl = fakeFetch(async () => json(saved));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  expect(await api.saveDraft(SESSION, 2, "With a quote", [], [PART])).toEqual(saved);
  expect(fetchImpl.mock.calls[0]?.[1]).toMatchObject({
    method: "PUT",
    body: JSON.stringify({
      expected_revision: 2,
      text: "With a quote",
      attachments: [],
      parts: [PART],
    }),
  });
});

test("an echo that lost the excerpts, or carries a malformed one, is a bad response", async () => {
  const fetchImpl = fakeFetch(async () => json({ ...draft, revision: 3, text: "With a quote" }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await expect(api.saveDraft(SESSION, 2, "With a quote", undefined, [PART])).rejects.toMatchObject({
    status: 0,
    code: "invalid_response",
  });
  // The parts key alone, with no attachments: the service's typed save needs neither to carry
  // the other.
  expect(String(fetchImpl.mock.calls[0]?.[1]?.body)).toBe(
    JSON.stringify({ expected_revision: 2, text: "With a quote", parts: [PART] }),
  );
  const malformed = fakeFetch(async () => json({ ...draft, parts: [{ ...PART, sha256: "nope" }] }));
  const strict = createDurableApi("secret", { fetch: malformed as typeof fetch });
  await expect(strict.getDraft(SESSION)).rejects.toMatchObject({ code: "invalid_response" });
});

test("a save with no excerpts keeps the legacy body, which is how the service clears them", async () => {
  const fetchImpl = fakeFetch(async () => json({ ...draft, revision: 3, text: "Text only" }));
  const api = createDurableApi("secret", { fetch: fetchImpl as typeof fetch });
  await api.saveDraft(SESSION, 2, "Text only", undefined, []);
  const body = String(fetchImpl.mock.calls[0]?.[1]?.body);
  expect(body).toBe(JSON.stringify({ expected_revision: 2, text: "Text only" }));
  expect(body).not.toContain("parts");
});
