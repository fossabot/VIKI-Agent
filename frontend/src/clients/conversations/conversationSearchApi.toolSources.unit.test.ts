import { afterEach, expect, test, vi } from "vitest";
import { createConversationSearchApi } from "./conversationSearchApi";

const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const REQUEST_ID = "22222222-2222-4222-8222-222222222222";

function json(value: unknown): Response {
  return Response.json(value);
}

function toolSourcesResponse(overrides: Record<string, unknown> = {}) {
  return {
    request_id: REQUEST_ID,
    state: "ready",
    reason: "",
    items: [],
    ...overrides,
  };
}

afterEach(() => vi.restoreAllMocks());

test("toolSources sends request with unique ids and parses a ready response", async () => {
  const fetchImpl = vi.fn(async () =>
    json(
      toolSourcesResponse({
        items: [
          { tool_use_id: "web-1", tool: "WebFetch", url: "https://example.com" },
          {
            tool_use_id: "grep-1",
            tool: "Grep",
            pattern: "test",
            path: "docs/",
            path_state: "workspace_relative",
          },
        ],
      }),
    ),
  );
  const api = createConversationSearchApi("secret", fetchImpl);

  const result = await api.toolSources?.(SESSION_ID, ["web-1", "grep-1"], REQUEST_ID);

  expect(fetchImpl).toHaveBeenCalledWith(
    `/api/v2/sessions/${SESSION_ID}/tool-sources`,
    expect.objectContaining({
      method: "POST",
      headers: {
        authorization: "Bearer secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        request_id: REQUEST_ID,
        tool_use_ids: ["web-1", "grep-1"],
      }),
    }),
  );
  expect(result).toEqual({
    request_id: REQUEST_ID,
    state: "ready",
    reason: "",
    items: [
      { tool_use_id: "web-1", tool: "WebFetch", url: "https://example.com" },
      {
        tool_use_id: "grep-1",
        tool: "Grep",
        pattern: "test",
        path: "docs/",
        path_state: "workspace_relative",
      },
    ],
  });
});

test("toolSources rejects invalid inputs before fetch", async () => {
  const fetchImpl = vi.fn();
  const api = createConversationSearchApi("secret", fetchImpl);

  const invalidCases: Array<[string[], string]> = [
    [[], "invalid_request"],
    [Array.from({ length: 17 }, (_, i) => `id-${i}`), "invalid_request"],
    [["x".repeat(161)], "invalid_request"],
    [[""], "invalid_request"],
  ];

  for (const [ids, expectedError] of invalidCases) {
    await expect(api.toolSources?.(SESSION_ID, ids, REQUEST_ID)).rejects.toThrow(expectedError);
  }
  expect(fetchImpl).not.toHaveBeenCalled();
});

test("toolSources deduplicates ids", async () => {
  const fetchImpl = vi.fn(async () =>
    json(
      toolSourcesResponse({
        items: [{ tool_use_id: "id-1", tool: "WebFetch", url: "https://x.com" }],
      }),
    ),
  );
  const api = createConversationSearchApi("secret", fetchImpl);

  await api.toolSources?.(SESSION_ID, ["id-1", "id-1", "id-1"], REQUEST_ID);

  expect(fetchImpl).toHaveBeenCalledWith(
    expect.anything(),
    expect.objectContaining({
      body: JSON.stringify({
        request_id: REQUEST_ID,
        tool_use_ids: ["id-1"],
      }),
    }),
  );
});

test("toolSources accepts ready, partial, missing, and error states", async () => {
  for (const response of [
    toolSourcesResponse({ state: "ready", reason: "" }),
    toolSourcesResponse({ state: "partial", reason: "outside_window" }),
    toolSourcesResponse({ state: "missing", reason: "transcript_missing" }),
    toolSourcesResponse({ state: "error", reason: "busy" }),
    toolSourcesResponse({ state: "error", reason: "read_failed" }),
    toolSourcesResponse({ state: "error", reason: "time_budget" }),
  ]) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(response)),
    );
    await expect(api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID)).resolves.toMatchObject({
      state: response.state,
      reason: response.reason,
    });
  }
});

test("toolSources rejects malformed responses", async () => {
  const malformed = [
    toolSourcesResponse({ request_id: SESSION_ID }),
    toolSourcesResponse({ state: "ready", reason: "busy" }),
    toolSourcesResponse({ state: "partial", reason: "transcript_missing" }),
    toolSourcesResponse({ state: "missing", reason: "busy" }),
    toolSourcesResponse({ state: "error", reason: "" }),
    toolSourcesResponse({ items: "not-an-array" }),
    toolSourcesResponse({ items: [{ tool_use_id: "id-1" }] }),
    toolSourcesResponse({ extra: "nope" }),
  ];

  for (const response of malformed) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(response)),
    );
    await expect(api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID)).rejects.toThrow(
      "invalid_response",
    );
  }
});

test("toolSources validates WebFetch items", async () => {
  const malformed = [
    { tool_use_id: "id-1", tool: "WebFetch", url: "http://example.com", pattern: "x" },
    { tool_use_id: "id-1", tool: "WebFetch", url: "javascript:alert(1)" },
    { tool_use_id: "id-1", tool: "WebFetch", url: "https://example.com/path with spaces" },
    { tool_use_id: "id-1", tool: "WebFetch", url: "https://example.com\x00bad" },
    { tool_use_id: "id-1", tool: "WebFetch", url: "ftp://example.com" },
    { tool_use_id: "id-1", tool: "WebFetch", url: "x".repeat(2049) },
    { tool_use_id: "id-1", tool: "WebFetch", url: "example.com" },
    { tool_use_id: "id-1", tool: "WebFetch", path: "" },
    { tool_use_id: "x".repeat(161), tool: "WebFetch", url: "https://x.com" },
    { tool_use_id: "id-1", tool: "WebFetch", url: "https://evil.example\\@docs.python.org/3/" },
    { tool_use_id: "id-1", tool: "WebFetch", url: "https://x.com", extra: "nope" },
  ];

  for (const item of malformed) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(toolSourcesResponse({ items: [item] }))),
    );
    await expect(api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID)).rejects.toThrow(
      "invalid_response",
    );
  }
});

test("toolSources validates Grep and Glob items", async () => {
  const malformed = [
    { tool_use_id: "id-1", tool: "Grep", pattern: "test", path: "docs/" },
    { tool_use_id: "id-1", tool: "Grep", pattern: "test", path: "docs/", path_state: "invalid" },
    {
      tool_use_id: "id-1",
      tool: "Grep",
      pattern: "test",
      path: "/docs/",
      path_state: "workspace_relative",
    },
    {
      tool_use_id: "id-1",
      tool: "Glob",
      pattern: "test",
      path: "docs\\file",
      path_state: "workspace_relative",
    },
    {
      tool_use_id: "id-1",
      tool: "Glob",
      pattern: "test",
      path: "docs:file",
      path_state: "workspace_relative",
    },
    {
      tool_use_id: "id-1",
      tool: "Grep",
      pattern: "test",
      path: ".",
      path_state: "workspace_relative",
    },
    {
      tool_use_id: "id-1",
      tool: "Grep",
      pattern: "test",
      path: "..",
      path_state: "workspace_relative",
    },
    {
      tool_use_id: "id-1",
      tool: "Grep",
      pattern: "test",
      path: "docs/./file",
      path_state: "workspace_relative",
    },
    { tool_use_id: "id-1", tool: "Glob", pattern: "test", path: "x", path_state: "root" },
    { tool_use_id: "id-1", tool: "Grep", pattern: "test", path: "x", path_state: "unknown" },
    {
      tool_use_id: "id-1",
      tool: "Grep",
      pattern: "x".repeat(161),
      path: "",
      path_state: "unknown",
    },
    {
      tool_use_id: "id-1",
      tool: "Grep",
      pattern: "test",
      path: "x".repeat(513),
      path_state: "workspace_relative",
    },
    {
      tool_use_id: "id-1",
      tool: "Grep",
      pattern: "test",
      url: "http://x.com",
      path: "",
      path_state: "unknown",
    },
    {
      tool_use_id: "id-1",
      tool: "Grep",
      pattern: "test",
      path: "",
      path_state: "unknown",
      extra: "nope",
    },
  ];

  for (const item of malformed) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () => json(toolSourcesResponse({ items: [item] }))),
    );
    await expect(api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID)).rejects.toThrow(
      "invalid_response",
    );
  }
});

test("toolSources accepts valid Grep and Glob paths", async () => {
  const valid = [
    { tool_use_id: "id-1", tool: "Grep", pattern: "test", path: "", path_state: "unknown" },
    {
      tool_use_id: "id-2",
      tool: "Glob",
      pattern: "*.md",
      path: "docs/",
      path_state: "workspace_relative",
    },
    { tool_use_id: "id-3", tool: "Grep", pattern: "error", path: "", path_state: "root" },
    {
      tool_use_id: "id-4",
      tool: "Glob",
      pattern: "**/*",
      path: "src/nested/deep",
      path_state: "workspace_relative",
    },
  ];

  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () => json(toolSourcesResponse({ items: valid }))),
  );
  const result = await api.toolSources?.(SESSION_ID, ["id-1", "id-2", "id-3", "id-4"], REQUEST_ID);
  expect(result?.items).toHaveLength(4);
});

test("toolSources rejects a response item for an id that was not asked", async () => {
  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () =>
      json(
        toolSourcesResponse({
          items: [
            { tool_use_id: "id-1", tool: "WebFetch", url: "https://x.com" },
            { tool_use_id: "id-2", tool: "Grep", pattern: "test", path: "", path_state: "unknown" },
            { tool_use_id: "id-999", tool: "WebFetch", url: "https://y.com" },
          ],
        }),
      ),
    ),
  );
  await expect(api.toolSources?.(SESSION_ID, ["id-1", "id-2"], REQUEST_ID)).rejects.toThrow(
    "invalid_response",
  );
});

test("toolSources rejects a second response item for the same id", async () => {
  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () =>
      json(
        toolSourcesResponse({
          items: [
            { tool_use_id: "id-1", tool: "WebFetch", url: "https://x.com" },
            { tool_use_id: "id-1", tool: "WebFetch", url: "https://y.com" },
          ],
        }),
      ),
    ),
  );
  await expect(api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID)).rejects.toThrow(
    "invalid_response",
  );
});

test("toolSources validates WebFetch URLs with strict rules", async () => {
  const validUrls = [
    "https://example.com",
    "http://example.com/path",
    "https://example.com:8080/path?query=1",
    "https://example.com/path#anchor",
  ];

  for (const url of validUrls) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () =>
        json(
          toolSourcesResponse({
            items: [{ tool_use_id: "id-1", tool: "WebFetch", url }],
          }),
        ),
      ),
    );
    const result = await api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID);
    const item = result?.items[0];
    if (item?.tool === "WebFetch") {
      expect(item.url).toBe(url);
    }
  }

  const invalidUrls = [
    "https://example.com/path\n",
    "https://example.com\t",
    "https://example.com/path\x00",
    "https://example.com/path\x1f",
    "https://example.com/path\x7f",
    "https://example.com/path\x85",
  ];

  for (const url of invalidUrls) {
    const api = createConversationSearchApi(
      "secret",
      vi.fn(async () =>
        json(
          toolSourcesResponse({
            items: [{ tool_use_id: "id-1", tool: "WebFetch", url }],
          }),
        ),
      ),
    );
    await expect(api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID)).rejects.toThrow(
      "invalid_response",
    );
  }
});

test("toolSources rejects response beyond byte budget before parsing", async () => {
  const body = JSON.stringify(toolSourcesResponse({ ignored: "x".repeat(512 * 1024) }));
  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () => new Response(body, { status: 200 })),
  );

  await expect(api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID)).rejects.toThrow(
    "response_too_large",
  );
});

test("toolSources accepts exact 64 KiB boundary", async () => {
  const items = Array.from({ length: 16 }, (_, i) => ({
    tool_use_id: `id${i}`,
    tool: "WebFetch" as const,
    url: "https://example.com/path" + "x".repeat(2000),
  }));
  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () => json(toolSourcesResponse({ items }))),
  );
  const result = await api.toolSources?.(
    SESSION_ID,
    items.map((i) => i.tool_use_id),
    REQUEST_ID,
  );
  expect(result?.items).toHaveLength(16);
});

test("toolSources accepts a legal batch between 64 KiB and 512 KiB", async () => {
  // The server's caps count code points, but its JSON escapes each astral character as a
  // 12-byte surrogate-pair escape (ensure_ascii). Sixteen legal Grep items at the pattern/path
  // caps, all astral, land well past 64 KiB on the wire while a legal batch must still be
  // accepted under the raised 512 KiB cap.
  const astralEscape = "\\uD83D\\uDE00";
  const ids = Array.from({ length: 16 }, (_, i) => `id${i}`);
  const itemsRaw = ids
    .map(
      (id) =>
        `{"tool_use_id":"${id}","tool":"Grep","pattern":"${astralEscape.repeat(160)}","path":"${astralEscape.repeat(512)}","path_state":"workspace_relative"}`,
    )
    .join(",");
  const body = `{"request_id":"${REQUEST_ID}","state":"ready","reason":"","items":[${itemsRaw}]}`;
  expect(body.length).toBeGreaterThan(64 * 1024);
  expect(body.length).toBeLessThanOrEqual(512 * 1024);

  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () => new Response(body, { status: 200 })),
  );
  const result = await api.toolSources?.(SESSION_ID, ids, REQUEST_ID);
  expect(result?.items).toHaveLength(16);
  const first = result?.items[0];
  if (first?.tool === "Grep") {
    expect([...first.pattern]).toHaveLength(160);
    expect([...first.path]).toHaveLength(512);
  }
});

test("toolSources handles Unicode in patterns and paths", async () => {
  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () =>
      json(
        toolSourcesResponse({
          items: [
            {
              tool_use_id: "id-1",
              tool: "Grep",
              pattern: "東京",
              path: "docs/",
              path_state: "workspace_relative",
            },
          ],
        }),
      ),
    ),
  );
  const result = await api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID);
  const item = result?.items[0];
  if (item?.tool === "Grep") {
    expect(item.pattern).toBe("東京");
  }
});

test("toolSources validates id bytes correctly", async () => {
  const emoji = "😀"; // 4 bytes in UTF-8
  const id = emoji.repeat(128); // 512 bytes
  const fetchImpl = vi.fn(async () =>
    json(
      toolSourcesResponse({
        items: [{ tool_use_id: id, tool: "WebFetch", url: "https://x.com" }],
      }),
    ),
  );
  const api = createConversationSearchApi("secret", fetchImpl);

  const result = await api.toolSources?.(SESSION_ID, [id], REQUEST_ID);
  expect(result?.items[0]?.tool_use_id).toBe(id);

  // One more emoji over the limit should fail
  const oversizeId = emoji.repeat(129);
  await expect(api.toolSources?.(SESSION_ID, [oversizeId], REQUEST_ID)).rejects.toThrow(
    "invalid_request",
  );
});

test("toolSources accepts empty items array", async () => {
  const api = createConversationSearchApi(
    "secret",
    vi.fn(async () => json(toolSourcesResponse({ items: [] }))),
  );
  const result = await api.toolSources?.(SESSION_ID, ["id-1"], REQUEST_ID);
  expect(result?.items).toEqual([]);
});
