import { afterEach, expect, test, vi } from "vitest";
import { createSubagentsApi } from "./subagentsApi";

const sessionId = "12345678-1234-1234-1234-123456789012";
const blockId = "toolu_01Abc-xyz_123";

afterEach(() => {
  vi.useRealTimers();
});

const validDocument = {
  prompt: "Explore the repo",
  lines: [
    { kind: "text", id: "t1", text: "thinking" },
    {
      kind: "tool",
      id: "to1",
      head: "Read",
      rest: "file.py",
      tail: "done",
      done: true,
      error: false,
    },
    { kind: "agent", id: "a1", agent: "Explore", description: "look around", status: "running" },
    { kind: "message", id: "m1", text: "a later note" },
  ],
  omitted: 0,
  partial: false,
  started_at: "2026-10-08T12:00:00.000Z",
  tokens: 42,
};

/** A fetch that never answers, and aborts the way the real one does when its signal fires. */
function hungFetch() {
  const signals: AbortSignal[] = [];
  const impl = ((_input: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) throw new Error("the GET carried no AbortSignal");
      signals.push(signal);
      signal.addEventListener("abort", () => reject(signal.reason));
    })) as typeof fetch;
  return { impl, signals };
}

test("read() sends only the bearer header to the exact path, and parses a full transcript", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${sessionId}/subagents/${blockId}`);
    expect(init?.method).toBe("GET");
    expect(init?.headers).toEqual({ authorization: "Bearer secret" });
    return Response.json(validDocument);
  };
  const api = createSubagentsApi("secret", request);
  const result = await api.read(sessionId, blockId);
  expect(result).toEqual({
    prompt: "Explore the repo",
    lines: validDocument.lines,
    omitted: 0,
    partial: false,
    startedAt: Date.parse(validDocument.started_at),
    tokens: 42,
  });
});

test("an empty started_at and null tokens parse to null", async () => {
  const request = async () =>
    Response.json({ ...validDocument, lines: [], started_at: "", tokens: null });
  const api = createSubagentsApi("secret", request);
  const result = await api.read(sessionId, blockId);
  expect(result.startedAt).toBeNull();
  expect(result.tokens).toBeNull();
});

test("an unparseable started_at parses to a null startedAt", async () => {
  const request = async () =>
    Response.json({ ...validDocument, lines: [], started_at: "not a date" });
  const api = createSubagentsApi("secret", request);
  const result = await api.read(sessionId, blockId);
  expect(result.startedAt).toBeNull();
});

test("a non-canonical session id is refused without a request", async () => {
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createSubagentsApi("secret", request);
  const error = await api.read("not-a-uuid", blockId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(Error);
  expect((error as Error).message).toBe("invalid_input");
});

test("a block id off the allowed pattern is refused without a request", async () => {
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createSubagentsApi("secret", request);
  for (const bad of ["", "has a space", "x".repeat(129), "toolu.bad"]) {
    const error = await api.read(sessionId, bad).catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe("invalid_input");
  }
});

test("a 404 answers not_found, a 429 answers overloaded, other non-2xx answers unavailable", async () => {
  const statusOf = async (status: number) => {
    const request = async () => new Response("", { status });
    const api = createSubagentsApi("secret", request);
    return (await api.read(sessionId, blockId).catch((thrown: unknown) => thrown)) as Error;
  };
  expect((await statusOf(404)).message).toBe("not_found");
  expect((await statusOf(429)).message).toBe("overloaded");
  expect((await statusOf(500)).message).toBe("unavailable");
  expect((await statusOf(403)).message).toBe("unavailable");
});

test("an extra or missing top-level key is refused", async () => {
  const api = createSubagentsApi("secret", async () =>
    Response.json({ ...validDocument, extra: 1 }),
  );
  const error = await api.read(sessionId, blockId).catch((thrown: unknown) => thrown);
  expect((error as Error).message).toBe("invalid_response");

  const { omitted: _omitted, ...withoutOmitted } = validDocument;
  const api2 = createSubagentsApi("secret", async () => Response.json(withoutOmitted));
  const error2 = await api2.read(sessionId, blockId).catch((thrown: unknown) => thrown);
  expect((error2 as Error).message).toBe("invalid_response");
});

test("a prompt over 65,536 units is refused; exactly 65,536 is not", async () => {
  const over = createSubagentsApi("secret", async () =>
    Response.json({ ...validDocument, lines: [], prompt: "x".repeat(65_537) }),
  );
  expect(
    ((await over.read(sessionId, blockId).catch((thrown: unknown) => thrown)) as Error).message,
  ).toBe("invalid_response");

  const atLimit = createSubagentsApi("secret", async () =>
    Response.json({ ...validDocument, lines: [], prompt: "x".repeat(65_536) }),
  );
  const result = await atLimit.read(sessionId, blockId);
  expect(result.prompt).toHaveLength(65_536);
});

test("more than 1,000 lines is refused", async () => {
  const lines = Array.from({ length: 1_001 }, (_, i) => ({
    kind: "text",
    id: `t${i}`,
    text: "x",
  }));
  const api = createSubagentsApi("secret", async () => Response.json({ ...validDocument, lines }));
  const error = await api.read(sessionId, blockId).catch((thrown: unknown) => thrown);
  expect((error as Error).message).toBe("invalid_response");
});

test("an unknown line kind is refused", async () => {
  const api = createSubagentsApi("secret", async () =>
    Response.json({ ...validDocument, lines: [{ kind: "other", id: "x" }] }),
  );
  const error = await api.read(sessionId, blockId).catch((thrown: unknown) => thrown);
  expect((error as Error).message).toBe("invalid_response");
});

test("a line missing a key, or carrying an extra one, for its kind is refused", async () => {
  const missingText = createSubagentsApi("secret", async () =>
    Response.json({ ...validDocument, lines: [{ kind: "text", id: "t1" }] }),
  );
  expect(
    ((await missingText.read(sessionId, blockId).catch((thrown: unknown) => thrown)) as Error)
      .message,
  ).toBe("invalid_response");

  const extraOnTool = createSubagentsApi("secret", async () =>
    Response.json({
      ...validDocument,
      lines: [
        {
          kind: "tool",
          id: "x",
          head: "h",
          rest: "r",
          tail: "t",
          done: true,
          error: false,
          extra: 1,
        },
      ],
    }),
  );
  expect(
    ((await extraOnTool.read(sessionId, blockId).catch((thrown: unknown) => thrown)) as Error)
      .message,
  ).toBe("invalid_response");
});

test("a non-text string field over 4,096 units is refused; a text/message text may reach 65,536", async () => {
  const idTooLong = createSubagentsApi("secret", async () =>
    Response.json({
      ...validDocument,
      lines: [{ kind: "text", id: "x".repeat(4_097), text: "ok" }],
    }),
  );
  expect(
    ((await idTooLong.read(sessionId, blockId).catch((thrown: unknown) => thrown)) as Error)
      .message,
  ).toBe("invalid_response");

  const longText = createSubagentsApi("secret", async () =>
    Response.json({
      ...validDocument,
      lines: [{ kind: "message", id: "m1", text: "x".repeat(65_536) }],
    }),
  );
  const result = await longText.read(sessionId, blockId);
  expect(result.lines).toEqual([{ kind: "message", id: "m1", text: "x".repeat(65_536) }]);
});

test("a non-boolean done/error on a tool line, or an unknown agent status, is refused", async () => {
  const badBool = createSubagentsApi("secret", async () =>
    Response.json({
      ...validDocument,
      lines: [
        { kind: "tool", id: "x", head: "h", rest: "r", tail: "t", done: "yes", error: false },
      ],
    }),
  );
  expect(
    ((await badBool.read(sessionId, blockId).catch((thrown: unknown) => thrown)) as Error).message,
  ).toBe("invalid_response");

  const badStatus = createSubagentsApi("secret", async () =>
    Response.json({
      ...validDocument,
      lines: [{ kind: "agent", id: "x", agent: "Explore", description: "d", status: "queued" }],
    }),
  );
  expect(
    ((await badStatus.read(sessionId, blockId).catch((thrown: unknown) => thrown)) as Error)
      .message,
  ).toBe("invalid_response");
});

test("omitted or tokens outside integer >= 0 is refused", async () => {
  const cases: Record<string, unknown>[] = [
    { ...validDocument, lines: [], omitted: -1 },
    { ...validDocument, lines: [], omitted: 1.5 },
    { ...validDocument, lines: [], tokens: -1 },
    { ...validDocument, lines: [], tokens: 1.5 },
    { ...validDocument, lines: [], tokens: true },
  ];
  for (const body of cases) {
    const api = createSubagentsApi("secret", async () => Response.json(body));
    const error = await api.read(sessionId, blockId).catch((thrown: unknown) => thrown);
    expect((error as Error).message).toBe("invalid_response");
  }
});

test("partial not a boolean is refused", async () => {
  const api = createSubagentsApi("secret", async () =>
    Response.json({ ...validDocument, lines: [], partial: "false" }),
  );
  const error = await api.read(sessionId, blockId).catch((thrown: unknown) => thrown);
  expect((error as Error).message).toBe("invalid_response");
});

test("caller abort rejects with cancelled", async () => {
  const { impl } = hungFetch();
  const api = createSubagentsApi("secret", impl);
  const controller = new AbortController();
  const outcome = api
    .read(sessionId, blockId, controller.signal)
    .catch((thrown: unknown) => thrown);
  controller.abort();
  const error = await outcome;
  expect(error).toBeInstanceOf(Error);
  expect((error as Error).message).toBe("cancelled");
});

test("a hung request times out at 15 s with unavailable", async () => {
  vi.useFakeTimers();
  const { impl, signals } = hungFetch();
  const api = createSubagentsApi("secret", impl);
  const outcome = api.read(sessionId, blockId).catch((thrown: unknown) => thrown);

  await vi.advanceTimersByTimeAsync(14_999);
  expect(signals[0]?.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(signals[0]?.aborted).toBe(true);

  const error = await outcome;
  expect(error).toBeInstanceOf(Error);
  expect((error as Error).message).toBe("unavailable");
});
