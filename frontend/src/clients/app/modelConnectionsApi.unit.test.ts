import { expect, test, vi } from "vitest";
import { createModelConnectionsApi, ModelConnectionError } from "./modelConnectionsApi";

const draft = {
  name: "Local",
  base_url: "http://127.0.0.1:11434/v1",
  api_mode: "chat_completions" as const,
  model: "example/model",
  api_key: "synthetic-api-key",
};
const listing = { revision: 1, connections: [], default_model: null };

test("credentials travel in the explicit save body, and the window token stays in its header", async () => {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify(listing), { status: 200 }));
  const api = createModelConnectionsApi("synthetic-window-token", fetchImpl);
  await api.save(0, draft);
  const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/v2/llm/connections");
  const token = "synthetic-window-token";
  expect(init.headers).toMatchObject({ authorization: `Bearer ${token}` });
  expect(init.body).not.toContain("synthetic-window-token");
  expect(JSON.parse(init.body as string)).toEqual({ expected_revision: 0, connection: draft });
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

test("a response that returns a secret is refused", async () => {
  const id = "11111111-1111-4111-8111-111111111111";
  const row = {
    ...draft,
    id,
    model_ref: `viki-llm/${id}/example/model`,
    has_api_key: true,
    is_default: false,
  };
  const api = createModelConnectionsApi(
    "token",
    vi.fn(async () => new Response(JSON.stringify({ ...listing, connections: [row] }))),
  );
  await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
});

test("provider error text never becomes an error message or a retry", async () => {
  const fetchImpl = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          error: { code: "authentication_failed", message: "echo synthetic-api-key" },
        }),
        { status: 400 },
      ),
  );
  const api = createModelConnectionsApi("token", fetchImpl);
  await expect(api.test(draft)).rejects.toEqual(new ModelConnectionError("authentication_failed"));
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

const id = "11111111-1111-4111-8111-111111111111";
function publicRow(name: string, model: string) {
  const { api_key: _key, ...publicDraft } = draft;
  return {
    ...publicDraft,
    id,
    name,
    model,
    model_ref: `viki-llm/${id}/${model}`,
    has_api_key: true,
    is_default: false,
  };
}

test.each(["😀".repeat(50), "😀".repeat(80)])(
  "listing accepts code-point name limits",
  async (name) => {
    const model = "😀".repeat(200) + "a".repeat(150); // 350 code points, 550 UTF-16 units, 950 bytes
    const api = createModelConnectionsApi(
      "token",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              ...listing,
              connections: [publicRow(name, model)],
            }),
          ),
      ),
    );
    expect((await api.list()).connections[0]).toMatchObject({ name, model });
  },
);

test.each([
  publicRow("😀".repeat(81), "model"),
  publicRow("name", "a".repeat(513)),
  publicRow("name", "😀".repeat(245)),
  publicRow("bad\nname", "model"),
  publicRow("name", "bad model"),
])("one invalid connection rejects the whole listing", async (row) => {
  const other = {
    ...publicRow("valid", "model"),
    id: "22222222-2222-4222-8222-222222222222",
    model_ref: "viki-llm/22222222-2222-4222-8222-222222222222/model",
  };
  const api = createModelConnectionsApi(
    "token",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            ...listing,
            connections: [other, row],
          }),
        ),
    ),
  );
  await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
});

test("discovery keeps valid Unicode model spelling and rejects byte/code-point overflow", async () => {
  const valid = "😀".repeat(200) + "a".repeat(150);
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ ok: true, models: [valid] })));
  const api = createModelConnectionsApi("token", fetchImpl);
  expect(await api.discover(draft)).toEqual([valid]);
  fetchImpl.mockImplementation(
    async () => new Response(JSON.stringify({ ok: true, models: ["😀".repeat(245)] })),
  );
  await expect(api.discover(draft)).rejects.toMatchObject({ code: "invalid_response" });
});

test("key renewal patches only the expected revision and new key at the saved identity", async () => {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify(listing)));
  const api = createModelConnectionsApi("token", fetchImpl);
  await api.updateKey(id, 4, "synthetic-replacement-key");
  const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe(`/api/v2/llm/connections/${id}`);
  expect(init.method).toBe("PATCH");
  expect(JSON.parse(init.body as string)).toEqual({
    expected_revision: 4,
    api_key: "synthetic-replacement-key",
  });
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

test("model validation follows Python whitespace and exact UTF-8 boundaries", async () => {
  let model = `${"a".repeat(174)}${"😀".repeat(201)}`; // exactly 978 UTF-8 bytes
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ ok: true, models: [model] })));
  const api = createModelConnectionsApi("token", fetchImpl);
  expect(await api.discover(draft)).toEqual([model]);
  model += "a";
  await expect(api.discover(draft)).rejects.toMatchObject({ code: "invalid_response" });
  model = "a\u0085b";
  await expect(api.discover(draft)).rejects.toMatchObject({ code: "invalid_response" });
  model = "a\u0080b";
  await expect(api.discover(draft)).rejects.toMatchObject({ code: "invalid_response" });
  model = "a\ufeffb";
  expect(await api.discover(draft)).toEqual([model]);
});

test("discovery accepts the bounded server's ASCII-escaped Unicode model list", async () => {
  const models = Array.from({ length: 256 }, (_, index) => `${"😀".repeat(200)}${index}`);
  const body = JSON.stringify({ ok: true, models }).replace(
    /[\u0080-\uffff]/g,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  expect(body.length).toBeGreaterThan(256 * 1024);
  const api = createModelConnectionsApi(
    "token",
    vi.fn(async () => new Response(body)),
  );
  expect(await api.discover(draft)).toEqual(models);
});
