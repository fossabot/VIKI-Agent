import { afterEach, expect, test, vi } from "vitest";
import { ContextApiError, createContextApi } from "./contextApi";

const sessionId = "12345678-1234-1234-1234-123456789012";

const validDocument = {
  observed_at: "2026-10-09T02:00:00.000Z",
  total_tokens: 1200,
  max_tokens: 200000,
  raw_max_tokens: 200000,
  percentage: 0.6,
  auto_compact: { enabled: true, threshold: 180000, source: "cli" },
  categories: [
    { name: "System prompt", tokens: 400, kind: "used" as const },
    { name: "Messages", tokens: 800, kind: "used" as const },
    { name: "Autocompact buffer", tokens: 2000, kind: "buffer" as const },
    { name: "Free space", tokens: 197000, kind: "free" as const },
  ],
  memory_files: [{ name: "CLAUDE.md", type: "Project", tokens: 300 }],
  mcp_servers: [{ server: "viki_echo", tools: 2, loaded: 1, loaded_tokens: 50 }],
  skills: {
    total: 2,
    included: 1,
    tokens: 20,
    items: [{ name: "example-skill", source: "project", plugin: null, tokens: 20 }],
  },
  agents: [{ type: "Explore", source: "built-in", tokens: 10 }],
  reasons: {},
  hidden: { categories: 0, memory_files: 0, mcp_servers: 0, skills: 0, agents: 0 },
};

afterEach(() => {
  vi.useRealTimers();
});

test("get() parses a full report and sends the token only in the header", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${sessionId}/context`);
    expect(init?.headers).toMatchObject({ authorization: "Bearer secret" });
    return Response.json(validDocument);
  };
  const api = createContextApi("secret", request);
  const result = await api.get(sessionId);
  expect(result).toEqual(validDocument);
});

test("a non-canonical session id is refused without a request", async () => {
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createContextApi("secret", request);
  const error = await api.get("not-a-uuid").catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("invalid_input");
});

test("an extra top-level key is refused", async () => {
  const request = async () => Response.json({ ...validDocument, extra: 1 });
  const api = createContextApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("invalid_response");
});

test("a report missing a required key is refused", async () => {
  const document = Object.fromEntries(
    Object.entries(validDocument).filter(([key]) => key !== "hidden"),
  );
  expect(Object.keys(document)).toHaveLength(12);
  const request = async () => Response.json(document);
  const api = createContextApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("invalid_response");
});

test("a float token count is refused", async () => {
  const request = async () => Response.json({ ...validDocument, total_tokens: 1200.5 });
  const api = createContextApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("invalid_response");
});

test("an overlong string is refused", async () => {
  const request = async () =>
    Response.json({
      ...validDocument,
      categories: [{ ...validDocument.categories[0], name: "x".repeat(257) }],
    });
  const api = createContextApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("invalid_response");
});

test("a reason on a listed (non-null) part is refused", async () => {
  const request = async () =>
    Response.json({ ...validDocument, reasons: { categories: "not_observed" } });
  const api = createContextApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("invalid_response");
});

test("a null part with no matching reason is refused", async () => {
  const request = async () => Response.json({ ...validDocument, mcp_servers: null, reasons: {} });
  const api = createContextApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("invalid_response");
});

test("a 409 not_running answer throws with the server's code", async () => {
  const request = async () =>
    new Response(JSON.stringify({ error: { code: "not_running" } }), { status: 409 });
  const api = createContextApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).status).toBe(409);
  expect((error as ContextApiError).code).toBe("not_running");
});

test("a slow response times out after 15 seconds", async () => {
  vi.useFakeTimers();
  const request = (_url: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
    });
  const api = createContextApi("secret", request);
  const pending = api.get(sessionId).catch((thrown: unknown) => thrown);
  await vi.advanceTimersByTimeAsync(15_000);
  const error = await pending;
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("timeout");
});

test("an external abort is refused as cancelled", async () => {
  const controller = new AbortController();
  const request = (_url: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
    });
  const api = createContextApi("secret", request);
  const pending = api.get(sessionId, controller.signal).catch((thrown: unknown) => thrown);
  controller.abort();
  const error = await pending;
  expect(error).toBeInstanceOf(ContextApiError);
  expect((error as ContextApiError).code).toBe("cancelled");
});
