import { afterEach, expect, test, vi } from "vitest";
import { createRunsApi, RunsApiError } from "./runsApi";

const sessionId = "12345678-1234-1234-1234-123456789012";
const blockId = "toolu_01Abc-xyz_123";

afterEach(() => {
  vi.useRealTimers();
});

/** Awaits a rejection and returns the thrown RunsApiError's code. */
async function codeOf(promise: Promise<unknown>): Promise<string> {
  const error = await promise.catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(RunsApiError);
  return (error as RunsApiError).code;
}

/** A fetch that never answers, and aborts the way the real one does when its signal fires. */
function hungFetch() {
  const signals: AbortSignal[] = [];
  const impl = ((_input: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) throw new Error("the call carried no AbortSignal");
      signals.push(signal);
      signal.addEventListener("abort", () => reject(signal.reason));
    })) as typeof fetch;
  return { impl, signals };
}

const validItem = {
  tool_use_id: "toolu_01Abc-xyz_123",
  run: "agent-7",
  raised_at: "2026-10-09T23:20:00.000Z",
};

test("requests() sends only the bearer header to the exact path, and parses a full list", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${sessionId}/requests`);
    expect(init?.method).toBe("GET");
    expect(init?.headers).toEqual({ authorization: "Bearer secret" });
    expect(init?.body).toBeUndefined();
    return Response.json({ requests: [validItem, { ...validItem, run: null }] });
  };
  const api = createRunsApi("secret", request);
  const result = await api.requests(sessionId);
  expect(result).toEqual([
    { toolUseId: "toolu_01Abc-xyz_123", run: "agent-7", raisedAt: Date.parse(validItem.raised_at) },
    { toolUseId: "toolu_01Abc-xyz_123", run: null, raisedAt: Date.parse(validItem.raised_at) },
  ]);
});

test("an empty list is fine", async () => {
  const api = createRunsApi("secret", async () => Response.json({ requests: [] }));
  expect(await api.requests(sessionId)).toEqual([]);
});

test("requests() answers not_found when the chat is not open", async () => {
  const api = createRunsApi(
    "secret",
    async () =>
      new Response(JSON.stringify({ error: { code: "not_found", message: "x" } }), {
        status: 404,
      }),
  );
  expect(await codeOf(api.requests(sessionId))).toBe("not_found");
});

test("a non-canonical session id is refused without a request, for both methods", async () => {
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createRunsApi("secret", request);
  expect(await codeOf(api.requests("not-a-uuid"))).toBe("invalid_input");
  expect(await codeOf(api.stop("not-a-uuid", blockId))).toBe("invalid_input");
});

test("a block id off the allowed pattern is refused without a request", async () => {
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createRunsApi("secret", request);
  for (const bad of ["", "has a space", "x".repeat(129), "toolu.bad"]) {
    expect(await codeOf(api.stop(sessionId, bad))).toBe("invalid_input");
  }
});

test("an extra or missing top-level key is refused", async () => {
  const withExtra = createRunsApi("secret", async () => Response.json({ requests: [], extra: 1 }));
  expect(await codeOf(withExtra.requests(sessionId))).toBe("invalid_response");

  const withoutKey = createRunsApi("secret", async () => Response.json({}));
  expect(await codeOf(withoutKey.requests(sessionId))).toBe("invalid_response");
});

test("more than 64 items is refused; exactly 64 is not", async () => {
  const over = createRunsApi("secret", async () =>
    Response.json({ requests: Array.from({ length: 65 }, () => validItem) }),
  );
  expect(await codeOf(over.requests(sessionId))).toBe("invalid_response");

  const atLimit = createRunsApi("secret", async () =>
    Response.json({ requests: Array.from({ length: 64 }, () => validItem) }),
  );
  expect(await atLimit.requests(sessionId)).toHaveLength(64);
});

test("an item missing a key, or carrying an extra one, is refused", async () => {
  const { run: _run, ...withoutRun } = validItem;
  const missing = createRunsApi("secret", async () => Response.json({ requests: [withoutRun] }));
  expect(await codeOf(missing.requests(sessionId))).toBe("invalid_response");

  const extra = createRunsApi("secret", async () =>
    Response.json({ requests: [{ ...validItem, extra: 1 }] }),
  );
  expect(await codeOf(extra.requests(sessionId))).toBe("invalid_response");
});

test("a tool_use_id or run over 128 characters is refused; exactly 128 is not", async () => {
  const longId = createRunsApi("secret", async () =>
    Response.json({ requests: [{ ...validItem, tool_use_id: "x".repeat(129) }] }),
  );
  expect(await codeOf(longId.requests(sessionId))).toBe("invalid_response");

  const atLimit = createRunsApi("secret", async () =>
    Response.json({ requests: [{ ...validItem, run: "x".repeat(128) }] }),
  );
  const result = await atLimit.requests(sessionId);
  expect(result[0]?.run).toHaveLength(128);
});

test("run must be a string or null, never another type", async () => {
  const api = createRunsApi("secret", async () =>
    Response.json({ requests: [{ ...validItem, run: 7 }] }),
  );
  expect(await codeOf(api.requests(sessionId))).toBe("invalid_response");
});

test("a raised_at not exactly 24 characters, or one Date.parse cannot read, is refused", async () => {
  const shortOne = createRunsApi("secret", async () =>
    Response.json({ requests: [{ ...validItem, raised_at: "2026-10-09T23:20:00.00Z" }] }),
  );
  expect(await codeOf(shortOne.requests(sessionId))).toBe("invalid_response");

  const unparseable = createRunsApi("secret", async () =>
    Response.json({ requests: [{ ...validItem, raised_at: "x".repeat(24) }] }),
  );
  expect(await codeOf(unparseable.requests(sessionId))).toBe("invalid_response");
});

test("stop() sends a bearer POST with no body to the exact path, and accepts {status: sent}", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${sessionId}/runs/${blockId}/stop`);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ authorization: "Bearer secret" });
    expect(init?.body).toBeUndefined();
    return Response.json({ status: "sent" });
  };
  const api = createRunsApi("secret", request);
  await expect(api.stop(sessionId, blockId)).resolves.toBeUndefined();
});

test("stop()'s 200 body must be exactly {status: sent}, else invalid_response", async () => {
  const extra = createRunsApi("secret", async () => Response.json({ status: "sent", extra: 1 }));
  expect(await codeOf(extra.stop(sessionId, blockId))).toBe("invalid_response");

  const wrongValue = createRunsApi("secret", async () => Response.json({ status: "ok" }));
  expect(await codeOf(wrongValue.stop(sessionId, blockId))).toBe("invalid_response");
});

test("a known error code in the body is kept, matching the route's status list", async () => {
  const codeFor = async (status: number, code: string) => {
    const api = createRunsApi(
      "secret",
      async () => new Response(JSON.stringify({ error: { code, message: "x" } }), { status }),
    );
    return codeOf(api.stop(sessionId, blockId));
  };
  expect(await codeFor(404, "not_found")).toBe("not_found");
  expect(await codeFor(409, "not_running")).toBe("not_running");
  expect(await codeFor(409, "unknown_run")).toBe("unknown_run");
  expect(await codeFor(409, "refused")).toBe("refused");
  expect(await codeFor(503, "timeout")).toBe("timeout");
  expect(await codeFor(429, "overloaded")).toBe("overloaded");
  expect(await codeFor(400, "invalid_input")).toBe("invalid_input");
});

test("an error status whose body carries no known code answers unavailable", async () => {
  const noBody = createRunsApi("secret", async () => new Response("", { status: 500 }));
  expect(await codeOf(noBody.stop(sessionId, blockId))).toBe("unavailable");

  const unknownCode = createRunsApi("secret", async () =>
    Response.json({ error: { code: "some_future_code" } }, { status: 409 }),
  );
  expect(await codeOf(unknownCode.stop(sessionId, blockId))).toBe("unavailable");

  // 401/403 are real server gates, but neither code is in this client's own vocabulary.
  const unauthorized = createRunsApi("secret", async () =>
    Response.json({ error: { code: "unauthorized" } }, { status: 401 }),
  );
  expect(await codeOf(unauthorized.stop(sessionId, blockId))).toBe("unavailable");
});

test("caller abort rejects requests() with cancelled", async () => {
  const { impl } = hungFetch();
  const api = createRunsApi("secret", impl);
  const controller = new AbortController();
  const outcome = codeOf(api.requests(sessionId, controller.signal));
  controller.abort();
  expect(await outcome).toBe("cancelled");
});

test("a hung requests() call times out at 15 s with unavailable", async () => {
  vi.useFakeTimers();
  const { impl, signals } = hungFetch();
  const api = createRunsApi("secret", impl);
  const outcome = codeOf(api.requests(sessionId));

  await vi.advanceTimersByTimeAsync(14_999);
  expect(signals[0]?.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(signals[0]?.aborted).toBe(true);

  expect(await outcome).toBe("unavailable");
});

test("a hung stop() call times out at 15 s with unavailable", async () => {
  vi.useFakeTimers();
  const { impl, signals } = hungFetch();
  const api = createRunsApi("secret", impl);
  const outcome = codeOf(api.stop(sessionId, blockId));

  await vi.advanceTimersByTimeAsync(15_000);
  expect(signals[0]?.aborted).toBe(true);

  expect(await outcome).toBe("unavailable");
});

test("a network failure answers unavailable (stop() has no signal to cancel it)", async () => {
  const api = createRunsApi("secret", async () => {
    throw new Error("network down");
  });
  expect(await codeOf(api.requests(sessionId))).toBe("unavailable");
  expect(await codeOf(api.stop(sessionId, blockId))).toBe("unavailable");
});
