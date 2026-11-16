import { afterEach, describe, expect, it, vi } from "vitest";
import { type AvatarManifest, createAvatarsApi } from "./avatarsApi";

const AVATAR = "00000000-0000-4000-8000-000000000001";

const manifest = (): AvatarManifest => ({
  avatar_id: AVATAR,
  master: { size: 512, byte_size: 1000, sha256: "a".repeat(64) },
  variants: [24, 32, 64, 128, 256].map((size) => ({
    size,
    byte_size: 100,
    sha256: "b".repeat(64),
  })),
  total_bytes: 1400,
  created_at: "2026-09-27T00:00:00.000Z",
});

const ok = (body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

const request = () => ({
  avatar_id: AVATAR,
  source_asset_id: "00000000-0000-4000-8000-000000000002",
  crop: { x: 0, y: 0, width: 10, height: 10 },
  rotation: 0 as const,
});

afterEach(() => vi.useRealTimers());

describe("avatars API", () => {
  it("transform posts JSON with bearer auth and returns a validated manifest", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(manifest()));
    const result = await createAvatarsApi("secret", fetcher).transform(request());
    expect(result.variants.map((variant) => variant.size)).toEqual([24, 32, 64, 128, 256]);
    expect(fetcher).toHaveBeenCalledWith(
      "/api/v2/avatars",
      expect.objectContaining({
        method: "POST",
        headers: { authorization: "Bearer secret", "content-type": "application/json" },
        body: JSON.stringify(request()),
      }),
    );
  });

  it("rejects a manifest missing a variant, out of order, or with a bad digest", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const bad = manifest();
    bad.variants = bad.variants.slice(1);
    fetcher.mockResolvedValueOnce(ok(bad));
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      code: "invalid_response",
    });
    const reordered = manifest();
    reordered.variants = [...reordered.variants].reverse();
    fetcher.mockResolvedValueOnce(ok(reordered));
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      code: "invalid_response",
    });
    const badDigest = manifest();
    badDigest.master.sha256 = "not-hex";
    fetcher.mockResolvedValueOnce(ok(badDigest));
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  it("keeps an allowlisted server code and falls back off-list per status class", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "invalid_crop" } }), { status: 400 }),
    );
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      status: 400,
      code: "invalid_crop",
    });
    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "disconnected" } }), { status: 400 }),
    );
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      status: 400,
      code: "disconnected",
    });
    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "not_a_real_code" } }), { status: 400 }),
    );
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      status: 400,
      code: "transform_failed",
    });
    fetcher.mockResolvedValueOnce(new Response("not json", { status: 500 }));
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      status: 500,
      code: "unavailable",
    });
  });

  it("admits the transform intake timeout and the decoder's unavailability", async () => {
    const fetcher = vi.fn<typeof fetch>();
    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "intake_timeout" } }), { status: 408 }),
    );
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      status: 408,
      code: "intake_timeout",
    });
    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "decoder_unavailable" } }), { status: 503 }),
    );
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      status: 503,
      code: "decoder_unavailable",
    });
    fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "not_a_real_code" } }), { status: 503 }),
    );
    await expect(createAvatarsApi("secret", fetcher).transform(request())).rejects.toMatchObject({
      status: 503,
      code: "unavailable",
    });
  });

  it("distinguishes caller cancellation from the client timeout", async () => {
    const cancelled = new AbortController();
    const fetcher = vi.fn<typeof fetch>((_url, init) => {
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    });
    const api = createAvatarsApi("secret", fetcher);
    const cancelling = expect(api.transform(request(), cancelled.signal)).rejects.toMatchObject({
      code: "cancelled",
    });
    cancelled.abort();
    await cancelling;

    vi.useFakeTimers();
    const timingOut = expect(api.transform(request())).rejects.toMatchObject({
      status: 0,
      code: "timeout",
    });
    await vi.advanceTimersByTimeAsync(35_000);
    await timingOut;
  });

  it("variant rejects a bad id or size before any request and maps a refusal", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const api = createAvatarsApi("secret", fetcher);
    await expect(api.variant("not-a-uuid", 24)).rejects.toMatchObject({ code: "invalid_avatar" });
    await expect(api.variant(AVATAR, 100 as never)).rejects.toMatchObject({
      code: "invalid_avatar",
    });
    expect(fetcher).not.toHaveBeenCalled();

    fetcher.mockResolvedValueOnce(new Response("", { status: 404 }));
    await expect(api.variant(AVATAR, 24)).rejects.toMatchObject({
      status: 404,
      code: "read_failed",
    });
  });

  it("variant reads with bearer auth and returns a bounded blob", async () => {
    const fetcher = vi.fn<typeof fetch>(async (url, init) => {
      expect(String(url)).toBe(`/api/v2/avatars/${AVATAR}/64`);
      expect(init?.headers).toEqual({ authorization: "Bearer secret" });
      return new Response(new Blob(["png"], { type: "image/png" }), { status: 200 });
    });
    const blob = await createAvatarsApi("secret", fetcher).variant(AVATAR, 64);
    expect(blob).toBeInstanceOf(Blob);
  });
});
