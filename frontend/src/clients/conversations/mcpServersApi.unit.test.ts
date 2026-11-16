import { afterEach, expect, test, vi } from "vitest";
import { createMcpServersApi, McpServersApiError } from "./mcpServersApi";

const sessionId = "12345678-1234-1234-1234-123456789012";

afterEach(() => {
  vi.useRealTimers();
});

/** Awaits a rejection and returns the thrown McpServersApiError's code. */
async function codeOf(promise: Promise<unknown>): Promise<string> {
  const error = await promise.catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(McpServersApiError);
  return (error as McpServersApiError).code;
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

test("setEnabled() sends a bearer POST with the exact JSON body to the toggle route", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${sessionId}/mcp/toggle`);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({
      authorization: "Bearer secret",
      "content-type": "application/json",
    });
    expect(init?.body).toBe(JSON.stringify({ name: "viki_echo", enabled: true }));
    return Response.json({ status: "done" });
  };
  const api = createMcpServersApi("secret", request);
  await expect(api.setEnabled(sessionId, "viki_echo", true)).resolves.toBeUndefined();
});

test("reconnect() sends a bearer POST with the exact JSON body to the reconnect route", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${sessionId}/mcp/reconnect`);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({
      authorization: "Bearer secret",
      "content-type": "application/json",
    });
    expect(init?.body).toBe(JSON.stringify({ name: "viki_echo" }));
    return Response.json({ status: "done" });
  };
  const api = createMcpServersApi("secret", request);
  await expect(api.reconnect(sessionId, "viki_echo")).resolves.toBeUndefined();
});

test("a non-canonical session id is refused without a request, for both methods", async () => {
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createMcpServersApi("secret", request);
  expect(await codeOf(api.setEnabled("not-a-uuid", "viki_echo", true))).toBe("invalid_input");
  expect(await codeOf(api.reconnect("not-a-uuid", "viki_echo"))).toBe("invalid_input");
});

test("a 200 body other than exactly {status: done} is refused", async () => {
  const extra = createMcpServersApi("secret", async () =>
    Response.json({ status: "done", extra: 1 }),
  );
  expect(await codeOf(extra.setEnabled(sessionId, "viki_echo", true))).toBe("invalid_response");

  const wrongValue = createMcpServersApi("secret", async () => Response.json({ status: "ok" }));
  expect(await codeOf(wrongValue.reconnect(sessionId, "viki_echo"))).toBe("invalid_response");
});

test("every gate and session code, including not_connected, is kept", async () => {
  const codeFor = async (status: number, code: string) => {
    const api = createMcpServersApi(
      "secret",
      async () => new Response(JSON.stringify({ error: { code, message: "x" } }), { status }),
    );
    return codeOf(api.reconnect(sessionId, "viki_echo"));
  };
  expect(await codeFor(401, "unauthorized")).toBe("unauthorized");
  expect(await codeFor(403, "forbidden_origin")).toBe("forbidden_origin");
  expect(await codeFor(400, "invalid_input")).toBe("invalid_input");
  expect(await codeFor(429, "overloaded")).toBe("overloaded");
  expect(await codeFor(404, "not_found")).toBe("not_found");
  expect(await codeFor(409, "not_running")).toBe("not_running");
  expect(await codeFor(409, "busy")).toBe("busy");
  expect(await codeFor(409, "unknown_server")).toBe("unknown_server");
  expect(await codeFor(409, "managed")).toBe("managed");
  expect(await codeFor(409, "not_approved")).toBe("not_approved");
  expect(await codeFor(409, "blocked")).toBe("blocked");
  expect(await codeFor(409, "refused")).toBe("refused");
  expect(await codeFor(409, "not_connected")).toBe("not_connected");
  expect(await codeFor(503, "timeout")).toBe("timeout");
});

test("an unknown server code, or a status with no body, answers unavailable", async () => {
  const noBody = createMcpServersApi("secret", async () => new Response("", { status: 500 }));
  expect(await codeOf(noBody.setEnabled(sessionId, "viki_echo", true))).toBe("unavailable");

  const unknownCode = createMcpServersApi("secret", async () =>
    Response.json({ error: { code: "some_future_code" } }, { status: 409 }),
  );
  expect(await codeOf(unknownCode.setEnabled(sessionId, "viki_echo", true))).toBe("unavailable");
});

test("a network failure answers unavailable", async () => {
  const api = createMcpServersApi("secret", async () => {
    throw new Error("network down");
  });
  expect(await codeOf(api.setEnabled(sessionId, "viki_echo", true))).toBe("unavailable");
});

test("a hung call times out at 60 s, not 30 s, with the timeout code", async () => {
  vi.useFakeTimers();
  const { impl, signals } = hungFetch();
  const api = createMcpServersApi("secret", impl);
  const outcome = codeOf(api.setEnabled(sessionId, "viki_echo", true));

  await vi.advanceTimersByTimeAsync(59_999);
  expect(signals[0]?.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(signals[0]?.aborted).toBe(true);

  expect(await outcome).toBe("timeout");
});
