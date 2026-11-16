import { afterEach, expect, test, vi } from "vitest";
import { BetweenChatsApiError, type ChatMessage, createBetweenChatsApi } from "./betweenChatsApi";

const validMessage: ChatMessage = {
  at: "2026-10-09T12:00:00.000Z",
  from_name: "VIKI",
  from_session_id: "11111111-1111-4111-8111-111111111111",
  to_session_id: "22222222-2222-4222-8222-222222222222",
  text: "hello from the other chat",
};

const validResult = { messages: [validMessage], partial: false };

afterEach(() => {
  vi.useRealTimers();
});

test("list() sends only the bearer token to the right path", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe("/api/v2/between-chats");
    expect(init?.method).toBe("GET");
    expect(init?.headers).toEqual({ authorization: "Bearer secret" });
    return Response.json(validResult);
  };
  const api = createBetweenChatsApi("secret", request);
  await api.list();
});

test("list() parses a full valid result", async () => {
  const request = async () => Response.json(validResult);
  const api = createBetweenChatsApi("secret", request);
  const result = await api.list();
  expect(result).toEqual(validResult);
});

test("from_session_id can be null", async () => {
  const request = async () =>
    Response.json({
      messages: [{ ...validMessage, from_session_id: null }],
      partial: false,
    });
  const api = createBetweenChatsApi("secret", request);
  const result = await api.list();
  expect(result.messages[0]?.from_session_id).toBeNull();
});

test("partial true is accepted", async () => {
  const request = async () => Response.json({ messages: [], partial: true });
  const api = createBetweenChatsApi("secret", request);
  const result = await api.list();
  expect(result).toEqual({ messages: [], partial: true });
});

test("exactly 20 messages are accepted", async () => {
  const messages = Array.from({ length: 20 }, () => validMessage);
  const request = async () => Response.json({ messages, partial: false });
  const api = createBetweenChatsApi("secret", request);
  const result = await api.list();
  expect(result.messages).toHaveLength(20);
});

test("21 messages are refused", async () => {
  const messages = Array.from({ length: 21 }, () => validMessage);
  const request = async () => Response.json({ messages, partial: false });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("messages must be an array", async () => {
  const request = async () => Response.json({ messages: {}, partial: false });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("partial must be a boolean", async () => {
  const request = async () => Response.json({ messages: [], partial: "false" });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("a missing top-level key is refused", async () => {
  const request = async () => Response.json({ messages: [] });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("an extra top-level key is refused", async () => {
  const request = async () => Response.json({ ...validResult, extra: 1 });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("a body that is not an object is refused", async () => {
  const bodies: unknown[] = [null, [], 42, "hello", true];
  for (const body of bodies) {
    const request = async () => Response.json(body);
    const api = createBetweenChatsApi("secret", request);
    const error = await api.list().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(BetweenChatsApiError);
    expect((error as BetweenChatsApiError).code).toBe("invalid_response");
  }
});

test("a message that is not an object is refused", async () => {
  const request = async () => Response.json({ messages: ["nope"], partial: false });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("a message missing a key is refused", async () => {
  const { text: _drop, ...rest } = validMessage;
  const request = async () => Response.json({ messages: [rest], partial: false });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("a message with an extra key is refused", async () => {
  const request = async () =>
    Response.json({ messages: [{ ...validMessage, extra: 1 }], partial: false });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("at must fullmatch the ISO shape", async () => {
  const bad = [
    "2026-10-09 12:00:00Z", // space instead of T
    "2026-10-09T12:00:00", // missing Z
    "2026-10-09T12:00:00+00:00", // offset instead of Z
    "2026-10-09T12:00:00.1234567Z", // 7 fractional digits
    "not-a-date",
  ];
  for (const at of bad) {
    const request = async () =>
      Response.json({ messages: [{ ...validMessage, at }], partial: false });
    const api = createBetweenChatsApi("secret", request);
    const error = await api.list().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(BetweenChatsApiError);
    expect((error as BetweenChatsApiError).code).toBe("invalid_response");
  }
});

test("at without a fraction is accepted", async () => {
  const request = async () =>
    Response.json({
      messages: [{ ...validMessage, at: "2026-10-09T12:00:00Z" }],
      partial: false,
    });
  const api = createBetweenChatsApi("secret", request);
  const result = await api.list();
  expect(result.messages[0]?.at).toBe("2026-10-09T12:00:00Z");
});

test("from_name must be 1..64 characters", async () => {
  const bad = ["", "a".repeat(65)];
  for (const from_name of bad) {
    const request = async () =>
      Response.json({ messages: [{ ...validMessage, from_name }], partial: false });
    const api = createBetweenChatsApi("secret", request);
    const error = await api.list().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(BetweenChatsApiError);
    expect((error as BetweenChatsApiError).code).toBe("invalid_response");
  }
});

test("from_name at 64 characters is accepted", async () => {
  const from_name = "a".repeat(64);
  const request = async () =>
    Response.json({ messages: [{ ...validMessage, from_name }], partial: false });
  const api = createBetweenChatsApi("secret", request);
  const result = await api.list();
  expect(result.messages[0]?.from_name).toBe(from_name);
});

test("to_session_id cannot be null", async () => {
  const request = async () =>
    Response.json({ messages: [{ ...validMessage, to_session_id: null }], partial: false });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("session ids must be canonical lowercase UUIDs", async () => {
  const bad = [
    "11111111-1111-4111-8111-11111111111",
    "aaaaaaaa-1111-4111-8111-111111111111".toUpperCase(),
    "not-a-uuid",
  ];
  for (const to_session_id of bad) {
    const request = async () =>
      Response.json({ messages: [{ ...validMessage, to_session_id }], partial: false });
    const api = createBetweenChatsApi("secret", request);
    const error = await api.list().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(BetweenChatsApiError);
    expect((error as BetweenChatsApiError).code).toBe("invalid_response");
  }
});

test("text must be <= 200 characters", async () => {
  const request = async () =>
    Response.json({
      messages: [{ ...validMessage, text: "a".repeat(201) }],
      partial: false,
    });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("invalid_response");
});

test("text at 200 characters is accepted", async () => {
  const text = "a".repeat(200);
  const request = async () =>
    Response.json({ messages: [{ ...validMessage, text }], partial: false });
  const api = createBetweenChatsApi("secret", request);
  const result = await api.list();
  expect(result.messages[0]?.text).toBe(text);
});

test("an oversized response is rejected before JSON parsing", async () => {
  const body = "x".repeat(70_000);
  const request = vi.fn(async () => new Response(body)) as typeof fetch;
  const parse = vi.spyOn(JSON, "parse");
  try {
    const api = createBetweenChatsApi("secret", request);
    const error = await api.list().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(BetweenChatsApiError);
    expect((error as BetweenChatsApiError).code).toBe("invalid_response");
    expect(parse).not.toHaveBeenCalled();
  } finally {
    parse.mockRestore();
  }
});

test("a 401 error uses the server code", async () => {
  const request = async () =>
    Response.json({ error: { code: "unauthorized" } }, { status: 401, statusText: "Unauthorized" });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("unauthorized");
});

test("a 500 error with no code defaults to unavailable", async () => {
  const request = async () =>
    Response.json({}, { status: 500, statusText: "Internal Server Error" });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("unavailable");
});

test("a server code that fails the allowlisted shape falls back to unavailable", async () => {
  const request = async () =>
    Response.json({ error: { code: "Not-Allowed!" } }, { status: 403, statusText: "Forbidden" });
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("unavailable");
});

test("a slow response times out after 15 seconds", async () => {
  vi.useFakeTimers();
  const request = (_url: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
    });
  const api = createBetweenChatsApi("secret", request);
  const pending = api.list().catch((thrown: unknown) => thrown);
  await vi.advanceTimersByTimeAsync(15_000);
  const error = await pending;
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("timeout");
});

test("a pre-aborted signal is immediately cancelled", async () => {
  const controller = new AbortController();
  controller.abort();
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list(controller.signal).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("cancelled");
});

test("a fetch failure is unavailable", async () => {
  const request = async () => {
    throw new Error("network down");
  };
  const api = createBetweenChatsApi("secret", request);
  const error = await api.list().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(BetweenChatsApiError);
  expect((error as BetweenChatsApiError).code).toBe("unavailable");
});
