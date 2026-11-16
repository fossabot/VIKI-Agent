import { describe, expect, it, vi } from "vitest";
import { createExcerptApi } from "./excerptApi";

const ID = "00000000-0000-4000-8000-000000000001";
const HASH = "a".repeat(64);
const reply = {
  part: {
    kind: "vault_excerpt",
    vault_id: ID,
    ref: "a/b.md",
    start_line: 2,
    end_line: 3,
    sha256: HASH,
  },
  text: "x\ny",
  block: "[vault excerpt] a/b.md, lines 2-3, sha256 aaaaaaaaaaaa\n```\nx\ny\n```",
  block_bytes: 62,
  revision: HASH,
  modified_at: "2026-10-01T00:00:00.000Z",
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

describe("excerpt API", () => {
  it("asks the excerpt route with the Bearer header, plain digits and a slash-kept ref", async () => {
    const fetchImpl = vi.fn(async () => json(reply));
    const result = await createExcerptApi("tok", fetchImpl).excerpt(ID, "日本/a b#.md", 2, 3);
    expect(result).toEqual(reply);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(
      `/api/v2/vaults/${ID}/excerpt?ref=${encodeURIComponent("日本")}/a%20b%23.md&start=2&end=3`,
    );
    expect(init.headers).toEqual({ authorization: "Bearer tok" });
  });

  it("raises the server's code, forbidden or not_found as a typed failure", async () => {
    const failed = (status: number, body: unknown) =>
      createExcerptApi("t", async () => json(body, status)).excerpt(ID, "a.md", 1, 2);
    for (const [status, code] of [
      [400, "invalid_range"],
      [413, "excerpt_too_large"],
      [409, "not_active_vault"],
      [429, "overloaded"],
    ] as const)
      await expect(failed(status, { error: { code, message: "m" } })).rejects.toThrow(code);
    await expect(failed(403, {})).rejects.toThrow("forbidden");
    await expect(failed(404, {})).rejects.toThrow("not_found");
    await expect(failed(503, "not json")).rejects.toThrow("unavailable");
  });

  it("refuses a bad id, ref or span before any request", async () => {
    const fetchImpl = vi.fn(async () => json(reply));
    const api = createExcerptApi("t", fetchImpl);
    await expect(api.excerpt("x", "a.md", 1, 2)).rejects.toThrow("invalid_workspace");
    await expect(api.excerpt(ID, "", 1, 2)).rejects.toThrow("invalid_ref");
    for (const [start, end] of [
      [0, 2],
      [3, 2],
      [1.5, 2],
      [1, 10_000_000],
    ])
      await expect(api.excerpt(ID, "a.md", start as number, end as number)).rejects.toThrow(
        "invalid_range",
      );
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects a reply whose field types are wrong", async () => {
    for (const bad of [
      { ...reply, text: 3 },
      { ...reply, block_bytes: "62" },
      { ...reply, part: { ...reply.part, sha256: "short" } },
      { ...reply, part: { ...reply.part, kind: "other" } },
      { ...reply, revision: undefined },
    ])
      await expect(
        createExcerptApi("t", async () => json(bad)).excerpt(ID, "a/b.md", 2, 3),
      ).rejects.toThrow("invalid_response");
  });

  it("cancels when the caller's signal is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(
      createExcerptApi("t", async () => json(reply)).excerpt(ID, "a.md", 1, 2, controller.signal),
    ).rejects.toThrow("cancelled");
  });
});
