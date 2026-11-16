import { afterEach, expect, test, vi } from "vitest";
import { createConversationSearchApi } from "./conversationSearchApi";

const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const REQUEST_ID = "22222222-2222-4222-8222-222222222222";
const ENTRY_UUID = "33333333-3333-4333-8333-333333333333";
const SOURCE_REVISION = "a".repeat(64);
const UNICODE_QUERY = "Stra\u00dfe \u6771\u4eac";
const anchor = {
  kind: "native_entry" as const,
  entry_uuid: ENTRY_UUID,
  mapped_index: 2,
  source_revision: SOURCE_REVISION,
};
const artifactRef = {
  entry_uuid: ENTRY_UUID,
  tool_use_id: "tool-1",
  source_revision: SOURCE_REVISION,
};

function json(value: unknown): Response {
  return Response.json(value);
}

function searchResponse(overrides: Record<string, unknown> = {}) {
  return {
    request_id: REQUEST_ID,
    state: "ready",
    reason: "",
    coverage: "complete",
    hits: [],
    ...overrides,
  };
}

function anchorResponse(overrides: Record<string, unknown> = {}) {
  return {
    request_id: REQUEST_ID,
    state: "ready",
    reason: "",
    events: [{ type: "user.message", data: { text: "found" } }],
    anchor_turn_index: 0,
    older_before_uuid: null,
    newer_after_uuid: null,
    coverage: "complete",
    ...overrides,
  };
}

function detailResponse(overrides: Record<string, unknown> = {}) {
  return {
    request_id: REQUEST_ID,
    state: "ready",
    reason: "",
    text: "detail",
    next_cursor: null,
    byte_size: 10,
    truncated: false,
    ...overrides,
  };
}

afterEach(() => vi.restoreAllMocks());

test("search encodes Unicode input and parses a revision-bound hit", async () => {
  const fetchImpl = vi.fn(async () =>
    json(
      searchResponse({
        hits: [
          {
            role: "assistant",
            at: "2026-09-21T00:00:00Z",
            snapshot: { before: "\u524d", match: UNICODE_QUERY, after: "\u5f8c" },
            anchor,
          },
        ],
      }),
    ),
  );
  const api = createConversationSearchApi("secret", fetchImpl);

  const result = await api.search(SESSION_ID, UNICODE_QUERY, REQUEST_ID);

  expect(fetchImpl).toHaveBeenCalledWith(
    `/api/v2/sessions/${SESSION_ID}/conversation-search?q=Stra%C3%9Fe%20%E6%9D%B1%E4%BA%AC&request_id=${REQUEST_ID}`,
    expect.objectContaining({
      method: "GET",
      headers: { authorization: "Bearer secret" },
    }),
  );
  expect(result.hits).toEqual([
    expect.objectContaining({
      role: "assistant",
      snapshot: { before: "\u524d", match: UNICODE_QUERY, after: "\u5f8c" },
      anchor,
    }),
  ]);
});

test("search rejects invalid inputs before fetch, including lone surrogates", async () => {
  const fetchImpl = vi.fn();
  const api = createConversationSearchApi("secret", fetchImpl);
  const invalidCases: Array<[string, string, string, string]> = [
    ["bad-session", "query", REQUEST_ID, "invalid_request"],
    [SESSION_ID, "query", "bad-request", "invalid_request"],
    [SESSION_ID, "   ", REQUEST_ID, "invalid_query"],
    [SESSION_ID, "a".repeat(257), REQUEST_ID, "invalid_query"],
    [SESSION_ID, String.fromCharCode(0xd800), REQUEST_ID, "invalid_query"],
  ];

  for (const [sessionId, query, requestId, error] of invalidCases) {
    await expect(api.search(sessionId, query, requestId)).rejects.toThrow(error);
  }
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("search accepts bounded partial and error results but rejects malformed responses", async () => {
  for (const response of [
    searchResponse({ state: "partial", reason: "tail_budget", coverage: "tail" }),
    searchResponse({ state: "partial", reason: "response_budget" }),
    searchResponse({ state: "partial", reason: "time_budget" }),
    searchResponse({ state: "partial", reason: "time_budget", coverage: "tail" }),
    searchResponse({ state: "error", reason: "busy" }),
  ]) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(response)),
    );
    await expect(api.search(SESSION_ID, "query", REQUEST_ID)).resolves.toMatchObject({
      state: response.state,
      reason: response.reason,
    });
  }

  const validHit = {
    role: "user",
    at: "now",
    snapshot: { before: "", match: "query", after: "" },
    anchor,
  };
  const malformed = [
    searchResponse({ request_id: SESSION_ID }),
    searchResponse({ state: "ready", reason: "busy" }),
    searchResponse({ state: "ready", coverage: "tail" }),
    searchResponse({ state: "partial", reason: "tail_budget" }),
    searchResponse({ state: "error", reason: "busy", hits: [validHit] }),
    searchResponse({ state: "superseded", reason: "cancelled", hits: [validHit] }),
    searchResponse({ hits: Array.from({ length: 101 }, () => validHit) }),
    searchResponse({ hits: [{ ...validHit, role: "system" }] }),
    searchResponse({
      hits: [{ ...validHit, snapshot: { before: "x".repeat(121), match: "query", after: "" } }],
    }),
    searchResponse({
      hits: [{ ...validHit, snapshot: { before: "", match: "x".repeat(257), after: "" } }],
    }),
    searchResponse({ hits: [{ ...validHit, anchor: { ...anchor, mapped_index: 64 } }] }),
    searchResponse({
      hits: [
        { ...validHit, anchor: { ...anchor, source_revision: SOURCE_REVISION.toUpperCase() } },
      ],
    }),
    searchResponse({ coverage: "all" }),
  ];
  for (const response of malformed) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(response)),
    );
    await expect(api.search(SESSION_ID, "query", REQUEST_ID)).rejects.toThrow("invalid_response");
  }
});

test("search rejects a response beyond its byte budget before parsing", async () => {
  const body = JSON.stringify(searchResponse({ ignored: "x".repeat(256 * 1024) }));
  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () => new Response(body, { status: 200 })),
  );

  await expect(api.search(SESSION_ID, "query", REQUEST_ID)).rejects.toThrow("response_too_large");
});

test("anchor sends its request ID and accepts only structurally consistent pages", async () => {
  const fetchImpl = vi.fn(async () => json(anchorResponse()));
  const api = createConversationSearchApi("secret", fetchImpl);

  await expect(api.anchor(SESSION_ID, anchor, REQUEST_ID)).resolves.toMatchObject({
    state: "ready",
    anchor_turn_index: 0,
  });
  expect(fetchImpl).toHaveBeenCalledWith(
    `/api/v2/sessions/${SESSION_ID}/history-anchor`,
    expect.objectContaining({
      method: "POST",
      headers: {
        authorization: "Bearer secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({ request_id: REQUEST_ID, anchor }),
    }),
  );

  for (const response of [
    anchorResponse({
      state: "partial",
      reason: "outside_window",
      events: [],
      anchor_turn_index: null,
      coverage: "tail",
    }),
    anchorResponse({ state: "error", reason: "busy", events: [], anchor_turn_index: null }),
    anchorResponse({ state: "error", reason: "time_budget", events: [], anchor_turn_index: null }),
  ]) {
    const scoped = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(response)),
    );
    await expect(scoped.anchor(SESSION_ID, anchor, REQUEST_ID)).resolves.toMatchObject({
      state: response.state,
      reason: response.reason,
    });
  }

  for (const response of [
    anchorResponse({ request_id: SESSION_ID }),
    anchorResponse({ state: "ready", reason: "busy" }),
    anchorResponse({ events: [], anchor_turn_index: 0 }),
    anchorResponse({ state: "missing", reason: "entry_missing" }),
    anchorResponse({
      state: "missing",
      reason: "entry_missing",
      events: [],
      anchor_turn_index: null,
      older_before_uuid: ENTRY_UUID,
    }),
    anchorResponse({
      state: "partial",
      reason: "outside_window",
      events: [],
      anchor_turn_index: null,
    }),
  ]) {
    const scoped = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(response)),
    );
    await expect(scoped.anchor(SESSION_ID, anchor, REQUEST_ID)).rejects.toThrow("invalid_response");
  }
});

test("artifact detail sends cursor and request ID and enforces UTF-8 and cursor bounds", async () => {
  const fetchImpl = vi.fn(async () =>
    json(detailResponse({ text: "\u65e5\u672c", byte_size: 16, next_cursor: 10 })),
  );
  const api = createConversationSearchApi("secret", fetchImpl);

  await expect(api.artifactDetail?.(SESSION_ID, artifactRef, 4, REQUEST_ID)).resolves.toMatchObject(
    {
      text: "\u65e5\u672c",
      next_cursor: 10,
    },
  );
  expect(fetchImpl).toHaveBeenCalledWith(
    `/api/v2/sessions/${SESSION_ID}/artifact-detail`,
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ request_id: REQUEST_ID, ref: artifactRef, cursor: 4 }),
    }),
  );

  for (const cursor of [-1, 0.5, 8 * 1024 * 1024 + 1]) {
    await expect(api.artifactDetail?.(SESSION_ID, artifactRef, cursor, REQUEST_ID)).rejects.toThrow(
      "invalid_request",
    );
  }
  await expect(
    api.artifactDetail?.(
      SESSION_ID,
      { ...artifactRef, tool_use_id: String.fromCodePoint(0x1f600).repeat(160) },
      0,
      REQUEST_ID,
    ),
  ).rejects.toThrow("invalid_request");

  const malformed = [
    detailResponse({ request_id: SESSION_ID }),
    detailResponse({ reason: "read_failed" }),
    detailResponse({ next_cursor: 4 }),
    detailResponse({ next_cursor: 8 * 1024 * 1024 + 1 }),
    detailResponse({ byte_size: 11, next_cursor: 9 }),
    detailResponse({ byte_size: 11 }),
    detailResponse({ truncated: true }),
    detailResponse({ text: "x".repeat(65537), byte_size: 65537 }),
    detailResponse({ text: "\u65e5".repeat(21846), byte_size: 65538 }),
    detailResponse({ state: "missing", reason: "entry_missing", text: "leak" }),
    detailResponse({
      state: "missing",
      reason: "entry_missing",
      text: "",
      byte_size: 1,
    }),
    detailResponse({
      state: "partial",
      reason: "outside_window",
      text: "",
      byte_size: 0,
      next_cursor: 10,
    }),
    detailResponse({
      state: "error",
      reason: "busy",
      text: "",
      byte_size: 0,
      truncated: true,
    }),
  ];
  for (const response of malformed) {
    const scoped = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(response)),
    );
    await expect(scoped.artifactDetail?.(SESSION_ID, artifactRef, 4, REQUEST_ID)).rejects.toThrow(
      "invalid_response",
    );
  }
});

test("artifact detail accepts the exact 64 KiB text boundary and unresolved empty pages", async () => {
  for (const response of [
    detailResponse({ text: "x".repeat(65536), byte_size: 65536 }),
    detailResponse({
      state: "partial",
      reason: "outside_window",
      text: "",
      next_cursor: null,
      byte_size: 0,
      truncated: false,
    }),
    detailResponse({
      state: "error",
      reason: "time_budget",
      text: "",
      next_cursor: null,
      byte_size: 0,
      truncated: false,
    }),
  ]) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(response)),
    );
    await expect(
      api.artifactDetail?.(SESSION_ID, artifactRef, 0, REQUEST_ID),
    ).resolves.toMatchObject({ state: response.state, text: response.text });
  }
});
