import { expect, test, vi } from "vitest";
import { createSessionMetadataApi } from "./sessionMetadataApi";

const id = "123e4567-e89b-12d3-a456-426614174000";
const row = { session_id: id, revision: 2, title: "Chat", pinned: false, archived: false };

test.each(["blue", "green", "orange", "purple", "red", "slate", null] as const)(
  "labels %s are revision checked without changing old fields",
  async (color) => {
    const changed = { ...row, revision: 3, unread: true, color };
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body))).toEqual({
        expected_revision: 2,
        patch: { unread: true, color },
      });
      return new Response(JSON.stringify(changed), { status: 200 });
    });
    const api = createSessionMetadataApi("token", { fetch: fetchImpl });
    expect(await api.patch(id, 2, { unread: true, color })).toEqual(changed);
    expect(fetchImpl).toHaveBeenCalledOnce();
  },
);

test.each([{ unread: 1 }, { unread: null }, { color: "amber" }, { color: false }])(
  "rejects malformed supplied labels %s",
  async (labels) => {
    const api = createSessionMetadataApi("token", {
      fetch: vi.fn(
        async () =>
          new Response(JSON.stringify({ sessions: [{ ...row, ...labels }] }), { status: 200 }),
      ),
    });
    await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
  },
);

test("a server that omits or changes the requested label cannot report success", async () => {
  const api = createSessionMetadataApi("token", {
    fetch: vi.fn(
      async () => new Response(JSON.stringify({ ...row, revision: 3 }), { status: 200 }),
    ),
  });
  await expect(api.patch(id, 2, { unread: true })).rejects.toMatchObject({
    code: "invalid_response",
  });
});

test("list and patch use bearer, origin-compatible relative paths, and CAS body", async () => {
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (String(url).endsWith("session-metadata"))
      return new Response(JSON.stringify({ sessions: [row] }), { status: 200 });
    expect(init?.method).toBe("PATCH");
    expect(init?.headers).toMatchObject({
      authorization: "Bearer token",
      "content-type": "application/json",
    });
    expect(JSON.parse(String(init?.body))).toEqual({
      expected_revision: 2,
      patch: { pinned: true },
    });
    return new Response(JSON.stringify({ ...row, revision: 3, pinned: true }), { status: 200 });
  });
  const api = createSessionMetadataApi("token", { fetch: fetchImpl });
  expect(await api.list()).toEqual([row]);
  expect(await api.patch(id, 2, { pinned: true })).toMatchObject({ revision: 3, pinned: true });
});

test("409 returns a revision conflict and never retries", async () => {
  const fetchImpl = vi.fn(
    async () =>
      new Response(JSON.stringify({ error: { code: "conflict", current_revision: 8 } }), {
        status: 409,
      }),
  );
  await expect(
    createSessionMetadataApi("token", { fetch: fetchImpl }).patch(id, 2, { title: "x" }),
  ).rejects.toMatchObject({ status: 409, code: "conflict", currentRevision: 8 });
  expect(fetchImpl).toHaveBeenCalledTimes(1);
});

test("an explicit create patch may use expected revision zero while listed rows remain revision one or greater", async () => {
  const initial = { session_id: id, revision: 1, title: "New", pinned: false, archived: false };
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      expect(JSON.parse(String(init.body))).toEqual({
        expected_revision: 0,
        patch: { title: "Renamed" },
      });
      return new Response(JSON.stringify({ ...initial, revision: 1, title: "Renamed" }), {
        status: 200,
      });
    }
    return new Response(JSON.stringify({ sessions: [initial] }), { status: 200 });
  });
  const api = createSessionMetadataApi("token", { fetch: fetchImpl });
  expect(await api.list()).toEqual([initial]);
  await expect(api.patch(id, 0, { title: "Renamed" })).resolves.toMatchObject({ revision: 1 });
});

test("remove sends a DELETE with the CAS body and 204 answers true", async () => {
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe(`/api/v2/session-metadata/${id}`);
    expect(init?.method).toBe("DELETE");
    expect(init?.headers).toMatchObject({
      authorization: "Bearer token",
      "content-type": "application/json",
    });
    expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 2 });
    return new Response(null, { status: 204 });
  });
  const api = createSessionMetadataApi("token", { fetch: fetchImpl });
  expect(await api.remove(id, 2)).toBe(true);
});

test("remove answers false on 404 without throwing", async () => {
  const fetchImpl = vi.fn(async () => new Response(null, { status: 404 }));
  const api = createSessionMetadataApi("token", { fetch: fetchImpl });
  expect(await api.remove(id, 2)).toBe(false);
});

test("remove throws a revision conflict on 409", async () => {
  const fetchImpl = vi.fn(
    async () =>
      new Response(JSON.stringify({ error: { code: "conflict", current_revision: 5 } }), {
        status: 409,
      }),
  );
  const api = createSessionMetadataApi("token", { fetch: fetchImpl });
  await expect(api.remove(id, 2)).rejects.toMatchObject({
    status: 409,
    code: "conflict",
    currentRevision: 5,
  });
});
