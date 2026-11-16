import { expect, test, vi } from "vitest";
import { createNativeAssetsApi, NativeAssetsApiError } from "./nativeAssetsApi";

const digest = "a".repeat(64);
const observedAt = "2026-09-28T00:00:00.000Z";
const validAsset = {
  source_id: "claude:user",
  source_scope: "user" as const,
  provider: "claude" as const,
  relative_id: ".claude/agents/example-agent.md",
  kind: "agent" as const,
  name: "example-agent",
  description: "An example agent.",
  model: null,
  tools: ["Read", "Grep"],
  digest,
  observed_at: observedAt,
  supported_fields: ["name", "description", "tools"],
  unsupported_fields: ["extra_key"],
  launch_supported: false as const,
};
const validCatalog = {
  source_id: "claude:user",
  provider: "claude" as const,
  source_scope: "user" as const,
  assets: [validAsset],
  partial: false,
  partial_reasons: [],
  scanned_entries: 1,
  scanned_bytes: 128,
  omitted: 0,
};

test("sources() parses a synthetic answer and sends the token only in the header", async () => {
  const request = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toBe("/api/v2/native-assets");
    expect(String(url)).not.toContain("secret");
    expect(init?.headers).toMatchObject({ authorization: "Bearer secret" });
    return Response.json({
      sources: [{ source_id: "claude:user", provider: "claude", scope: "user" }],
    });
  });
  const api = createNativeAssetsApi("secret", request);
  const result = await api.sources();
  expect(result).toEqual([{ source_id: "claude:user", provider: "claude", scope: "user" }]);
});

test("catalog() parses a synthetic catalog and encodes the source id", async () => {
  const request = vi.fn(async (url: RequestInfo | URL) => {
    expect(String(url)).toBe("/api/v2/native-assets?source_id=claude%3Auser");
    return Response.json(validCatalog);
  });
  const api = createNativeAssetsApi("secret", request);
  const result = await api.catalog("claude:user");
  expect(result.assets[0]?.name).toBe("example-agent");
});

test("an extra key on an asset is refused", async () => {
  const request = vi.fn(async () =>
    Response.json({
      ...validCatalog,
      assets: [{ ...validAsset, extra: "no" }],
    }),
  );
  const api = createNativeAssetsApi("secret", request);
  const error = await api.catalog("claude:user").catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(NativeAssetsApiError);
  expect((error as NativeAssetsApiError).code).toBe("invalid_response");
});

test("a bad digest is refused", async () => {
  const request = vi.fn(async () =>
    Response.json({
      ...validCatalog,
      assets: [{ ...validAsset, digest: "not-hex" }],
    }),
  );
  const api = createNativeAssetsApi("secret", request);
  const error = await api.catalog("claude:user").catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(NativeAssetsApiError);
  expect((error as NativeAssetsApiError).code).toBe("invalid_response");
});

test("an omitted count past the plain entry cap, within the partial-reason bound, is accepted", async () => {
  const request = vi.fn(async () =>
    Response.json({
      ...validCatalog,
      partial: true,
      partial_reasons: ["entry_limit"],
      omitted: 257,
    }),
  );
  const api = createNativeAssetsApi("secret", request);
  const result = await api.catalog("claude:user");
  expect(result.omitted).toBe(257);
});

test("an extra top-level key on the sources answer is refused", async () => {
  const request = vi.fn(async () =>
    Response.json({
      sources: [{ source_id: "claude:user", provider: "claude", scope: "user" }],
      extra: 1,
    }),
  );
  const api = createNativeAssetsApi("secret", request);
  const error = await api.sources().catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(NativeAssetsApiError);
  expect((error as NativeAssetsApiError).code).toBe("invalid_response");
});

test("a 409 source_unavailable answer throws with the server's code", async () => {
  const request = vi.fn(
    async () =>
      new Response(JSON.stringify({ error: { code: "source_unavailable" } }), { status: 409 }),
  );
  const api = createNativeAssetsApi("secret", request);
  const error = await api.catalog("claude:user").catch((thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(NativeAssetsApiError);
  expect((error as NativeAssetsApiError).status).toBe(409);
  expect((error as NativeAssetsApiError).code).toBe("source_unavailable");
});
