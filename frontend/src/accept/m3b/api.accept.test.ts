/** Acceptance (frozen): the REST client, against a fetch stub. */
import { expect, test } from "vitest";
import { createRest, RestError } from "../../api";
import { emptyRails } from "./fakes";

function fetchStub(status: number, body: unknown) {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const impl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    const text = typeof body === "string" ? body : JSON.stringify(body);
    return new Response(text, { status, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  return { impl, calls };
}

test("every call is a GET with the Bearer header and the token nowhere else", async () => {
  const { impl, calls } = fetchStub(200, { sessions: [], hits: [], ...emptyRails() });
  const rest = createRest("tok-secret", impl);
  await rest.sessions();
  await rest.search("x");
  await rest.rails();
  expect(calls.map((c) => c.url)).toEqual(["/api/sessions", "/api/search?q=x", "/api/rails"]);
  for (const call of calls) {
    expect(call.init?.method ?? "GET").toBe("GET");
    expect(new Headers(call.init?.headers).get("authorization")).toBe("Bearer tok-secret");
    expect(call.url).not.toContain("tok-secret");
  }
});

test("sessions(), search() and rails() return their bodies", async () => {
  const row = {
    session_id: "a",
    title: "t",
    first_prompt: "p",
    entrypoint: "cli",
    modified: "2026-09-12T08:00:00Z",
    cli_version: "2.1.259",
    other: false,
  };
  const listing = { sessions: [row], state: "ready", reason: "", omitted: 0 };
  expect(await createRest("tok", fetchStub(200, listing).impl).sessions()).toEqual(listing);
  const hit = { session_id: "a", title: "t", role: "user", snippet: "s", at: "" };
  const result = { hits: [hit], state: "ready", reason: "", scanned: 1, total: 1, request_id: "" };
  expect(await createRest("tok", fetchStub(200, result).impl).search("s")).toEqual(result);
  const rails = { ...emptyRails(), counts: { knowledge: 1, sources: 2, inbox: 3 } };
  expect(await createRest("tok", fetchStub(200, rails).impl).rails()).toEqual(rails);
});

test("the query is percent-encoded and passed whole, spaces included", async () => {
  const { impl, calls } = fetchStub(200, { hits: [] });
  await createRest("tok", impl).search(" 牛乳 買う ");
  expect(calls[0]?.url).toBe(`/api/search?q=${encodeURIComponent(" 牛乳 買う ")}`);
});

test("a non-2xx response rejects with RestError carrying the status and the body", async () => {
  const { impl } = fetchStub(401, { error: "unauthorized" });
  const error = await createRest("tok", impl)
    .sessions()
    .catch((e: unknown) => e);
  expect(error).toBeInstanceOf(RestError);
  expect((error as RestError).status).toBe(401);
  expect((error as RestError).body).toContain("unauthorized");
});

test("a failed fetch rejects with the fetch error unchanged", async () => {
  const boom = new TypeError("Failed to fetch");
  const impl = (async () => {
    throw boom;
  }) as unknown as typeof fetch;
  await expect(createRest("tok", impl).rails()).rejects.toBe(boom);
});
