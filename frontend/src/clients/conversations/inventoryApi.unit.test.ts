import { expect, test } from "vitest";
import { createInventoryApi, InventoryApiError } from "./inventoryApi";

const sessionId = "12345678-1234-1234-1234-123456789012";

const validDocument = {
  observed_at: "2026-09-28T02:00:00.000Z",
  mcp_servers: [
    {
      name: "viki_echo",
      status: "connected" as const,
      scope: "dynamic",
      source: "dynamic",
      server_name: "viki-echo",
      server_version: "1",
      egress: null,
      tools: ["echo"],
      tools_omitted: false,
    },
  ],
  mcp_tools: [{ name: "mcp__viki_echo__echo", server: "viki_echo", loaded: true }],
  skills: ["example-skill"],
  memory_files: [{ path: "%VAULT%/CLAUDE.md", type: "Project" }],
  system_tools: ["Read"],
  agents: [{ type: "Explore", source: "built-in" }],
  reasons: {},
};

test("get() parses a full inventory and sends the token only in the header", async () => {
  const request = async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/sessions/${sessionId}/inventory`);
    expect(init?.headers).toMatchObject({ authorization: "Bearer secret" });
    return Response.json(validDocument);
  };
  const api = createInventoryApi("secret", request);
  const result = await api.get(sessionId);
  expect(result).toEqual(validDocument);
});

test("a non-canonical session id is refused without a request", async () => {
  const request = async () => {
    throw new Error("should not be called");
  };
  const api = createInventoryApi("secret", request);
  const error = await api.get("not-a-uuid").catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(InventoryApiError);
  expect((error as InventoryApiError).code).toBe("invalid_input");
});

test("a null part with no matching reason is refused", async () => {
  const request = async () => Response.json({ ...validDocument, mcp_servers: null, reasons: {} });
  const api = createInventoryApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(InventoryApiError);
  expect((error as InventoryApiError).code).toBe("invalid_response");
});

test("an unknown server status is refused", async () => {
  const request = async () =>
    Response.json({
      ...validDocument,
      mcp_servers: [{ ...validDocument.mcp_servers[0], status: "odd" }],
    });
  const api = createInventoryApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(InventoryApiError);
  expect((error as InventoryApiError).code).toBe("invalid_response");
});

test("a server row without its source key is refused", async () => {
  const row = Object.fromEntries(
    Object.entries({ ...validDocument.mcp_servers[0] }).filter(([key]) => key !== "source"),
  );
  expect(Object.keys(row)).toHaveLength(8);
  const request = async () => Response.json({ ...validDocument, mcp_servers: [row] });
  const api = createInventoryApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(InventoryApiError);
  expect((error as InventoryApiError).code).toBe("invalid_response");
});

test("a server row whose source is not a string is refused", async () => {
  const request = async () =>
    Response.json({
      ...validDocument,
      mcp_servers: [{ ...validDocument.mcp_servers[0], source: 7 }],
    });
  const api = createInventoryApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(InventoryApiError);
  expect((error as InventoryApiError).code).toBe("invalid_response");
});

test("an extra top-level key is refused", async () => {
  const request = async () => Response.json({ ...validDocument, extra: 1 });
  const api = createInventoryApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(InventoryApiError);
  expect((error as InventoryApiError).code).toBe("invalid_response");
});

test("a 409 not_running answer throws with the server's code", async () => {
  const request = async () =>
    new Response(JSON.stringify({ error: { code: "not_running" } }), { status: 409 });
  const api = createInventoryApi("secret", request);
  const error = await api.get(sessionId).catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(InventoryApiError);
  expect((error as InventoryApiError).status).toBe(409);
  expect((error as InventoryApiError).code).toBe("not_running");
});
