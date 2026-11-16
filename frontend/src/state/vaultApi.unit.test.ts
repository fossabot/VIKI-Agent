import { describe, expect, it, vi } from "vitest";
import {
  createVaultApi,
  type VaultNote,
  type VaultNoteHistory,
  type VaultReadApi,
  VaultWriteError,
  validWriteRef,
} from "./vaultApi";

const ID = "00000000-0000-4000-8000-000000000001";
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

// The hygiene guard scans this file, so the credential file name is built from halves rather
// than written whole (AGENTS.md).
const CREDENTIALS_JSON = "." + "credentials" + ".json";
const WRITE_SET_MEMBERS = [
  ".claude",
  ".codex",
  ".git",
  ".viki",
  "credentials",
  "credentials.json",
  CREDENTIALS_JSON,
  "secrets.json",
  ".env",
  ".env.local",
  ".env.foo",
  "agents.md",
  "agents.override.md",
  "claude.md",
  "claude.local.md",
  "gemini.md",
  ".viki-stage-x",
  ".viki-backup-x",
];
const mixCase = (value: string): string =>
  Array.from(value)
    .map((character, index) =>
      index % 2 === 0 ? character.toUpperCase() : character.toLowerCase(),
    )
    .join("");

describe("vault API", () => {
  it("uses the listing and search bounds and refuses lone surrogates", async () => {
    for (const [count, query, accepted] of [
      [5000, undefined, true],
      [5001, undefined, false],
      [100, "query", true],
      [101, "query", false],
    ] as const) {
      const rows = Array.from({ length: count }, (_, i) => ({ ref: `note-${i}.md` }));
      const api = createVaultApi(
        "token",
        vi
          .fn<typeof fetch>()
          .mockResolvedValue(ok(query === undefined ? { entries: rows } : { hits: rows })),
      );
      if (accepted) await expect(api.entries(ID, query)).resolves.toBeDefined();
      else await expect(api.entries(ID, query)).rejects.toThrow("invalid_response");
    }
    const fetcher = vi.fn<typeof fetch>();
    await expect(createVaultApi("token", fetcher).entries(ID, "\ud800")).rejects.toThrow(
      "invalid_query",
    );
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("builds every route under the base it is given", async () => {
    const note = { note: { ref: "a.md", text: "x" }, resolved_links: [] };
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url) => {
      const path = String(url);
      if (path.endsWith("/metadata"))
        return ok({ items: [], partial: false, partial_reasons: [], read_bytes: 0, omitted: 0 });
      return ok(path.includes("/notes/") ? note : { entries: [], hits: [] });
    });
    const base = "/api/v2/bot-memory";
    const api = createVaultApi("token", fetcher, base);
    await api.entries(ID);
    await api.note(ID, "a.md");
    await api.history(ID, "a.md").catch(() => undefined);
    await api.bookmarks(ID);
    await api.addBookmark(ID, "a.md").catch(() => undefined);
    await api.createNote(ID, 1, "1:2", "a.md", "x").catch(() => undefined);
    await api
      .saveInstruction?.(ID, 1, "1:2", "CLAUDE.md", "x", "a".repeat(64))
      .catch(() => undefined);
    await api.metadata?.(ID, ["a.md"]).catch(() => undefined);
    const urls = fetcher.mock.calls.map((call) => String(call[0]));
    expect(urls.length).toBe(8);
    for (const url of urls) expect(url.startsWith(`${base}/${ID}/`)).toBe(true);
    const defaults = vi.fn<typeof fetch>().mockResolvedValue(ok({ entries: [] }));
    await createVaultApi("token", defaults).entries(ID);
    expect(defaults.mock.calls[0]?.[0]).toBe(`/api/v2/vaults/${ID}/entries`);
  });
  it("encodes a search query and rejects unsafe refs", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok({ entries: [] }));
    const api = createVaultApi("token", fetcher);
    await api.entries(ID, "tea & coffee");
    expect(fetcher.mock.calls[0]?.[0]).toContain("q=tea%20%26%20coffee");
    await expect(api.note(ID, "../private.md")).rejects.toThrow("invalid_ref");
  });
  it("rejects malformed JSON shapes and oversized streams", async () => {
    const api = createVaultApi("token", vi.fn<typeof fetch>().mockResolvedValue(ok({ nope: [] })));
    await expect(api.entries(ID)).rejects.toThrow("invalid_response");
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new Uint8Array(13 * 1024 * 1024 + 1));
        controller.close();
      },
    });
    const large = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(new Response(stream)),
    );
    await expect(large.entries(ID)).rejects.toThrow("response_too_large");
  });
  it("aborts a pending request at the bounded timeout", async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    const api = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockImplementation((_url, init) => {
        signal = (init as RequestInit).signal ?? undefined;
        return new Promise(() => {});
      }),
    );
    void api.entries(ID);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(signal?.aborted).toBe(true);
    vi.useRealTimers();
  });
  it("creates and saves Markdown with an exact policy snapshot", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() =>
      Promise.resolve(
        ok({
          operation_id: "00000000-0000-4000-8000-000000000002",
          status: "saved",
          cleanup_pending: false,
        }),
      ),
    );
    const api = createVaultApi("token", fetcher);
    await api.createNote(ID, 3, "12:34", "notes/tea.md", "# Tea\n");
    await api.saveNote(ID, 3, "12:34", "notes/tea.md", "# Milk tea\n", "a".repeat(64));
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[0]?.[0]).toContain("/notes/notes/tea.md");
    const create = JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body));
    const save = JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body));
    expect(create).toEqual({
      policy_revision: 3,
      root_physical_id: "12:34",
      content: "# Tea\n",
      expected_sha256: null,
    });
    expect(save.expected_sha256).toBe("a".repeat(64));
    expect(new Headers(fetcher.mock.calls[0]?.[1]?.headers).get("authorization")).toBe(
      "Bearer token",
    );
  });
  it("rejects unsafe writes locally before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const api = createVaultApi("token", fetcher);
    await expect(api.createNote(ID, 3, "12:34", "note.txt", "x")).rejects.toThrow(
      "invalid_publication",
    );
    await expect(api.createNote(ID, 3, "12:34", "note.md", "\ud800")).rejects.toThrow(
      "invalid_publication",
    );
    await expect(api.createNote(ID, 3, "12:34", "note.md", "bad\u0000markdown")).rejects.toThrow(
      "invalid_publication",
    );
    await expect(api.createNote(ID, 3, "12:34", ".git/note.md", "x")).rejects.toThrow(
      "invalid_publication",
    );
    await expect(api.createNote(ID, 3, "12:34", "bot-memory/MEMORY.md", "x")).rejects.toThrow(
      "invalid_publication",
    );
    await expect(
      api.createNote(ID, 3, "12:34", "note.md", "x".repeat(2 * 1024 * 1024 + 1)),
    ).rejects.toThrow("invalid_publication");
    await expect(api.saveNote(ID, 3, "12:34", "note.md", "x", "A".repeat(64))).rejects.toThrow(
      "invalid_publication",
    );
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("returns conflicts and preserves recovery operation IDs", async () => {
    const conflict = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          JSON.stringify({
            operation_id: "00000000-0000-4000-8000-000000000002",
            status: "conflict",
            cleanup_pending: true,
          }),
          { status: 409 },
        ),
      ),
    );
    await expect(conflict.createNote(ID, 3, "12:34", "note.md", "x")).resolves.toMatchObject({
      status: "conflict",
      cleanup_pending: true,
    });

    const recovery = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "recovery_required",
              message: "safe",
              operation_id: "00000000-0000-4000-8000-000000000003",
            },
          }),
          { status: 503 },
        ),
      ),
    );
    const error = await recovery
      .createNote(ID, 3, "12:34", "note.md", "x")
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(VaultWriteError);
    expect(error).toMatchObject({
      code: "recovery_required",
      operationId: "00000000-0000-4000-8000-000000000003",
    });
  });
  it("carries the server's error code for a failed read", async () => {
    const api = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ error: { code: "target_busy", message: "busy" } }), {
          status: 423,
        }),
      ),
    );
    await expect(api.note(ID, "note.md")).rejects.toThrow("target_busy");
  });
  it("falls back to a generic read failure without a parseable error code", async () => {
    const api = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(new Response("not json", { status: 503 })),
    );
    await expect(api.entries(ID)).rejects.toThrow("unavailable");
  });
  it("reads bookmarks and history from their paths, encoding the ref as note does", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => Promise.resolve(ok({})));
    const api = createVaultApi("token", fetcher);
    const ref = "メモ/a b#c.md";
    const encoded = "%E3%83%A1%E3%83%A2/a%20b%23c.md";
    await api.bookmarks(ID);
    await api.history(ID, ref);
    await api.addBookmark(ID, ref);
    await api.removeBookmark(ID, ref);
    const urls = fetcher.mock.calls.map((call) => call[0]);
    expect(urls).toEqual([
      `/api/v2/vaults/${ID}/bookmarks`,
      `/api/v2/vaults/${ID}/history/${encoded}`,
      `/api/v2/vaults/${ID}/bookmarks/${encoded}`,
      `/api/v2/vaults/${ID}/bookmarks/${encoded}`,
    ]);
    expect(fetcher.mock.calls.map((call) => call[1]?.method ?? "GET")).toEqual([
      "GET",
      "GET",
      "PUT",
      "DELETE",
    ]);
    for (const call of fetcher.mock.calls)
      expect(new Headers(call[1]?.headers).get("authorization")).toBe("Bearer token");
    for (const call of fetcher.mock.calls.slice(2)) {
      expect(call[1]?.body).toBeUndefined();
      expect(new Headers(call[1]?.headers).has("content-type")).toBe(false);
    }
  });
  it("types a history commit's at as the ISO-8601 string the server sends", async () => {
    const wire: VaultNoteHistory = {
      state: "ready",
      commits: [{ short: "abc1234", at: "2026-10-02T13:15:00+09:00", subject: "s" }],
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(wire));
    const history = await createVaultApi("token", fetcher).history(ID, "a.md");
    expect(history).toEqual(wire);
    expect(typeof wire.commits[0]?.at).toBe("string");
    expect(Number.isNaN(Date.parse(wire.commits[0]?.at ?? ""))).toBe(false);
  });
  it("rejects an unsafe bookmark or history ref before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const api = createVaultApi("token", fetcher);
    await expect(api.history(ID, "../x.md")).rejects.toThrow("invalid_ref");
    await expect(api.addBookmark(ID, "../x.md")).rejects.toThrow("invalid_ref");
    await expect(api.removeBookmark(ID, "/x.md")).rejects.toThrow("invalid_ref");
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("turns a typed bookmark or history failure into an error with its code", async () => {
    const failed = (code: string, status: number) =>
      createVaultApi(
        "token",
        vi
          .fn<typeof fetch>()
          .mockImplementation(() =>
            Promise.resolve(
              new Response(JSON.stringify({ error: { code, message: "m" } }), { status }),
            ),
          ),
      );
    await expect(failed("bookmarks_full", 409).addBookmark(ID, "a.md")).rejects.toThrow(
      "bookmarks_full",
    );
    await expect(failed("invalid_representation", 415).removeBookmark(ID, "a.md")).rejects.toThrow(
      "invalid_representation",
    );
    await expect(failed("overloaded", 429).bookmarks(ID)).rejects.toThrow("overloaded");
    await expect(failed("unavailable", 503).history(ID, "a.md")).rejects.toThrow("unavailable");
  });
  it("accepts only bounded redacted recovery rows", async () => {
    const api = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(
        ok({
          publications: [
            {
              operation_id: "00000000-0000-4000-8000-000000000002",
              workspace_id: ID,
              ref: "note.md",
              state: "backed_up",
              reason: "recovery_required",
              resolvable: ["drop_previous"],
            },
          ],
        }),
      ),
    );
    await expect(api.recovery()).resolves.toEqual([
      expect.objectContaining({ state: "backed_up", reason: "recovery_required" }),
    ]);
  });
  it.each([
    ["a missing list", undefined],
    ["an unknown choice", ["forget", "wipe"]],
    ["a duplicate", ["forget", "forget"]],
    ["a non-array", "forget"],
    ["three entries", ["forget", "drop_previous", "forget"]],
  ])("refuses a recovery row with %s in resolvable", async (_name, resolvable) => {
    const row = {
      operation_id: "00000000-0000-4000-8000-000000000002",
      workspace_id: ID,
      ref: "note.md",
      state: "backed_up",
      reason: "recovery_required",
      resolvable,
    };
    const api = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(ok({ publications: [row] })),
    );
    await expect(api.recovery()).rejects.toThrow("invalid_response");
  });
});

describe("saveInstruction", () => {
  const OP = "00000000-0000-4000-8000-000000000002";
  const SHA = "b".repeat(64);
  const outcome = (status: string, sha256: unknown, code = 200) =>
    new Response(JSON.stringify({ operation_id: OP, status, cleanup_pending: false, sha256 }), {
      status: code,
    });
  it("PUTs the exact request to the instructions route", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(outcome("saved", SHA));
    const api = createVaultApi("token", fetcher);
    const saved = await api.saveInstruction?.(
      ID,
      3,
      "12:34",
      "CLAUDE.md",
      "# Rules\n",
      "a".repeat(64),
    );
    expect(saved).toEqual({
      operation_id: OP,
      status: "saved",
      cleanup_pending: false,
      sha256: SHA,
    });
    expect(fetcher.mock.calls[0]?.[0]).toBe(`/api/v2/vaults/${ID}/instructions/CLAUDE.md`);
    const init = fetcher.mock.calls[0]?.[1];
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual({
      policy_revision: 3,
      root_physical_id: "12:34",
      content: "# Rules\n",
      expected_sha256: "a".repeat(64),
    });
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer token");
    expect(new Headers(init?.headers).get("content-type")).toBe("application/json");
  });
  it("refuses bad names, hashes, content and policy snapshots before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const save = createVaultApi("token", fetcher).saveInstruction;
    const a = "a".repeat(64);
    for (const call of [
      () => save?.(ID, 3, "12:34", "agents.md" as "AGENTS.md", "x", a),
      () => save?.(ID, 3, "12:34", "sub/AGENTS.md" as "AGENTS.md", "x", a),
      () => save?.(ID, 3, "12:34", "AGENTS.md", "x", "A".repeat(64)),
      () => save?.(ID, 3, "12:34", "AGENTS.md", "bad\u0000text", a),
      () => save?.(ID, 3, "12:34", "AGENTS.md", "x".repeat(2 * 1024 * 1024 + 1), a),
      () => save?.(ID, 0, "12:34", "AGENTS.md", "x", a),
      () => save?.(ID, 3, "bad", "AGENTS.md", "x", a),
      () => save?.("nope", 3, "12:34", "AGENTS.md", "x", a),
    ])
      await expect(call()).rejects.toThrow("invalid_publication");
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("returns a conflict with a null hash and no_change with one", async () => {
    const a = "a".repeat(64);
    const conflict = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(outcome("conflict", null, 409)),
    );
    await expect(
      conflict.saveInstruction?.(ID, 3, "12:34", "AGENTS.md", "x", a),
    ).resolves.toMatchObject({ status: "conflict", sha256: null });
    const same = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(outcome("no_change", SHA)),
    );
    await expect(
      same.saveInstruction?.(ID, 3, "12:34", "AGENTS.md", "x", a),
    ).resolves.toMatchObject({ status: "no_change", sha256: SHA });
  });
  it("rejects a saved answer without a valid hash and keeps error codes", async () => {
    const a = "a".repeat(64);
    for (const sha256 of [null, "short", undefined]) {
      const api = createVaultApi(
        "token",
        vi.fn<typeof fetch>().mockResolvedValue(outcome("saved", sha256)),
      );
      await expect(api.saveInstruction?.(ID, 3, "12:34", "AGENTS.md", "x", a)).rejects.toThrow(
        "invalid_response",
      );
    }
    const failed = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          JSON.stringify({ error: { code: "unsupported_filesystem", operation_id: OP } }),
          {
            status: 422,
          },
        ),
      ),
    );
    await expect(
      failed.saveInstruction?.(ID, 3, "12:34", "AGENTS.md", "x", a),
    ).rejects.toMatchObject({
      code: "unsupported_filesystem",
      operationId: OP,
    });
  });
});

describe("validWriteRef", () => {
  it("still refuses both instruction names as notes", () => {
    expect(validWriteRef("AGENTS.md")).toBe(false);
    expect(validWriteRef("CLAUDE.md")).toBe(false);
  });
  it("refuses every write-set member at depth 1 and 2, in mixed case", () => {
    for (const member of WRITE_SET_MEMBERS) {
      const mixed = mixCase(member);
      expect(validWriteRef(`${mixed}/x.md`)).toBe(false);
      expect(validWriteRef(`sub/${mixed}/x.md`)).toBe(false);
    }
  });
  it("refuses bad characters, reserved leaves, device stems and malformed paths", () => {
    for (const ref of [
      "what?.md",
      `${"a".repeat(253)}.md`, // 256 UTF-16 units
      ".md",
      "..md",
      "x.markdown",
      "COM1.md",
      "nul.md",
      "a./b.md",
      "a /b.md",
      "a//b.md",
      "a/./b.md",
      // Dotless ı: JS toUpperCase(), like Python's .upper(), maps it to I, so this device stem
      // is still reserved even though toLowerCase() alone would not catch it.
      "conın$.md",
    ])
      expect(validWriteRef(ref)).toBe(false);
  });
  it("accepts ordinary refs, a 255-unit leaf and non-ASCII segments", () => {
    expect(validWriteRef("notes/ok.md")).toBe(true);
    expect(validWriteRef("sub/note.md")).toBe(true);
    expect(validWriteRef("notes/bot-memory/note.md")).toBe(true);
    expect(validWriteRef(`${"a".repeat(252)}.md`)).toBe(true); // 255 UTF-16 units
    expect(validWriteRef("メモ/猫.md")).toBe(true);
  });
  it("refuses the bot-memory root for every write destination, case-insensitively", () => {
    for (const ref of ["bot-memory/MEMORY.md", "Bot-Memory/topic.md", "BOT-MEMORY/new.md"])
      expect(validWriteRef(ref)).toBe(false);
  });
  it("accepts COM0 and LPT0, which NTFS creates but Python 3.14 does not reserve (B-17)", () => {
    expect(validWriteRef("COM0.md")).toBe(true);
    expect(validWriteRef("LPT0.md")).toBe(true);
  });
  it("counts ref length in code points, as the server's len() does", () => {
    // Each astral character is one code point but two UTF-16 units, so a ref that packs 512
    // code points of astral characters is far over 512 UTF-16 units. Path segments stay under
    // the 255-UTF-16-unit NTFS part limit by holding at most 100 astral characters each.
    const astral = "\u{1F600}";
    const buildAstralRef = (codePoints: number): string => {
      const perSegment = 100;
      const segments: string[] = [];
      let remaining = codePoints;
      while (remaining > 0) {
        const take = Math.min(perSegment, remaining);
        segments.push(astral.repeat(take));
        remaining -= take;
      }
      return `${segments.join("/")}.md`;
    };
    const atLimit = buildAstralRef(504); // 504 astral chars + 5 slashes + ".md" = 512 code points
    const overLimit = buildAstralRef(505); // 513 code points
    expect([...atLimit].length).toBe(512);
    expect([...overLimit].length).toBe(513);
    expect(validWriteRef(atLimit)).toBe(true);
    expect(validWriteRef(overLimit)).toBe(false);
  });
});

describe("selected-note display metadata transport", () => {
  const ref = "notes/名前.md";
  const note: VaultNote = {
    ref,
    title: "名前",
    text: "---\r\ntitle: SKILL\r\n---\r\n# Body\r\n[[target]]",
    source: "\ufeff---\r\ntitle: SKILL\r\n---\r\n# Body\r\n[[target]]",
    byte_size: 45,
    sha256: "14f947b814ae91d479dfb774f18d3d909d23f40f54226b197c15f18ebab81428",
    frontmatter: { title: "SKILL" },
    links: ["target"],
    frontmatter_error: null,
  };
  const metadata = {
    state: "ready",
    frontmatter_state: "ready",
    heading_state: "ready",
    display_title: "SKILL",
    title_source: "title",
    tags: [],
    type: null,
    status: null,
  };
  const response = (display_metadata: unknown) => ({
    note: { ...note, display_metadata },
    resolved_links: [{ target: "target", ref: "target.md", state: "resolved" }],
  });

  it("keeps old omitted-field responses and the exact source/hash/ref request contract", async () => {
    const body = { note, resolved_links: [] };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(body));
    const controller = new AbortController();
    const result = await createVaultApi("token", fetcher).note(ID, ref, controller.signal);
    expect(result).toEqual(body);
    expect(Object.hasOwn(result.note as object, "display_metadata")).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const call = fetcher.mock.calls[0];
    expect(call?.[0]).toBe(`/api/v2/vaults/${ID}/notes/notes/%E5%90%8D%E5%89%8D.md`);
    expect(new Headers(call?.[1]?.headers).get("authorization")).toBe("Bearer token");
    expect(call?.[1]?.signal).toBeInstanceOf(AbortSignal);
    expect(call?.[1]?.body).toBeUndefined();
  });

  it("preserves a valid projection and every legacy field", async () => {
    const body = response(metadata);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(body));
    await expect(createVaultApi("token", fetcher).note(ID, ref)).resolves.toEqual(body);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["null", null],
    ["array", []],
    ["scalar", "ready"],
    ["missing key", { ...metadata, status: undefined }],
    ["extra key", { ...metadata, extra: true }],
    ["invalid state", { ...metadata, state: "unknown" }],
    [
      "untrusted H1",
      { ...metadata, title_source: "h1", heading_state: "unsupported", state: "unsupported" },
    ],
    ["duplicate tags", { ...metadata, tags: ["same", "same"] }],
    ["oversized title", { ...metadata, display_title: "🧠".repeat(513) }],
  ])("rejects a present projection with %s", async (_name, display_metadata) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(response(display_metadata)));
    await expect(createVaultApi("token", fetcher).note(ID, ref)).rejects.toThrow(
      "invalid_response",
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not let valid metadata bypass the existing note response checks", async () => {
    for (const body of [
      {
        ...response(metadata),
        note: { ...note, ref: "../private.md", display_metadata: metadata },
      },
      { ...response(metadata), note: { ...note, text: false, display_metadata: metadata } },
      { note: { ...note, display_metadata: metadata } },
    ]) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(body));
      await expect(createVaultApi("token", fetcher).note(ID, ref)).rejects.toThrow(
        "invalid_response",
      );
    }
  });
});

describe("bounded metadata batch transport", () => {
  const method = (api: ReturnType<typeof createVaultApi>) => {
    if (!api.metadata) throw new Error("metadata_method_missing");
    return api.metadata;
  };
  const refs = ["notes/日本語.md", "other.MARKDOWN"];
  const projection = {
    state: "ready",
    frontmatter_state: "ready",
    heading_state: "absent",
    display_title: "SKILL",
    title_source: "title",
    tags: [],
    type: null,
    status: null,
  };
  const wire = () => ({
    items: refs.map((ref) => ({
      ref,
      state: "ready",
      reason: null,
      read_bytes: 12,
      display_metadata: projection,
    })),
    partial: false,
    partial_reasons: [],
    read_bytes: 24,
    omitted: 0,
  });

  it("keeps old read fakes valid and posts exact refs through existing private request plumbing", async () => {
    const old: VaultReadApi = {
      entries: async () => ({}),
      note: async () => ({}),
      backlinks: async () => ({}),
      history: async () => ({}),
    };
    expect(old.metadata).toBeUndefined();
    const body = wire();
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(body));
    const controller = new AbortController();
    const result = await method(createVaultApi("token", fetcher))(ID, refs, controller.signal);
    expect(result).toEqual(body);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, init] = fetcher.mock.calls[0] ?? [];
    expect(url).toBe(`/api/v2/vaults/${ID}/metadata`);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ refs });
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer token");
    expect(new Headers(init?.headers).get("content-type")).toBe("application/json");
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("rejects invalid batches before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const api = createVaultApi("token", fetcher);
    for (const invalid of [
      [],
      Array.from({ length: 33 }, (_, i) => `${i}.md`),
      ["a.md", "a.md"],
      ["../a.md"],
      ["a//b.md"],
      ["a.txt"],
      [".md"],
      ["x\u007f.md"],
      ["x\ud800.md"],
      [`${"🧠".repeat(510)}.md`],
    ])
      await expect(method(api)(ID, invalid)).rejects.toThrow("invalid_metadata_request");
    await expect(method(api)("wrong", ["a.md"])).rejects.toThrow("invalid_workspace");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects extra keys, ref order and lying byte totals", async () => {
    for (const body of [
      { ...wire(), extra: true },
      { ...wire(), items: wire().items.reverse() },
      { ...wire(), read_bytes: 0 },
    ]) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(body));
      await expect(method(createVaultApi("token", fetcher))(ID, refs)).rejects.toThrow(
        "invalid_response",
      );
      expect(fetcher).toHaveBeenCalledTimes(1);
    }
  });
});

const batchProjection = {
  state: "ready",
  frontmatter_state: "ready",
  heading_state: "absent",
  display_title: "Title",
  title_source: "title",
  tags: ["tag"],
  type: null,
  status: null,
};
const batchItem = (changes: Record<string, unknown> = {}) => ({
  ref: "a.md",
  state: "ready",
  reason: null,
  read_bytes: 12,
  display_metadata: batchProjection,
  ...changes,
});
const batchWire = (changes: Record<string, unknown> = {}) => ({
  items: [batchItem()],
  partial: false,
  partial_reasons: [],
  read_bytes: 12,
  omitted: 0,
  ...changes,
});
const fetchMetadata = (body: unknown, refs = ["a.md"]) => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(body));
  const metadata = createVaultApi("token", fetcher).metadata;
  if (!metadata) throw new Error("metadata_method_missing");
  return { fetcher, promise: metadata(ID, refs) };
};

describe("metadata batch outcome correlations", () => {
  it("rejects malformed envelopes and every item/projection charge mismatch", async () => {
    const badItems = [
      null,
      false,
      {},
      batchItem({ extra: true }),
      batchItem({ ref: "other.md" }),
      batchItem({ state: undefined }),
      batchItem({ display_metadata: undefined }),
      batchItem({ display_metadata: null }),
      batchItem({ state: "absent" }),
      batchItem({ state: "invented" }),
      batchItem({ state: "changed", reason: "changed" }),
      batchItem({ reason: "prefix_limit" }),
      batchItem({ read_bytes: -1 }),
      batchItem({ read_bytes: 32769 }),
      batchItem({ read_bytes: 0.5 }),
      batchItem({ read_bytes: "12" }),
      batchItem({ read_bytes: false }),
      batchItem({ display_metadata: { ...batchProjection, tags: ["same", "same"] } }),
      batchItem({ display_metadata: { ...batchProjection, display_title: "🧠".repeat(513) } }),
      batchItem({ display_metadata: { ...batchProjection, extra: "hash" } }),
      batchItem({
        state: "unreadable",
        reason: "private_path",
        read_bytes: 1,
        display_metadata: null,
      }),
      batchItem({ state: "unreadable", reason: "not_admitted", display_metadata: null }),
      batchItem({ state: "changed", reason: "read_failed", display_metadata: null }),
      batchItem({ state: "unknown", reason: "missing_note", display_metadata: null }),
    ];
    const bad = [
      null,
      [],
      "ready",
      {},
      batchWire({ items: [] }),
      batchWire({ items: [batchItem(), batchItem()] }),
      batchWire({ partial: "false" }),
      batchWire({ partial: true }),
      batchWire({ partial_reasons: ["unsupported"] }),
      batchWire({ omitted: 1 }),
      batchWire({ omitted: false }),
      batchWire({ read_bytes: 12.5 }),
      batchWire({ read_bytes: "12" }),
      batchWire({ extra: "source" }),
      ...badItems.map((item) => batchWire({ items: [item] })),
    ];
    for (const body of bad) {
      const { fetcher, promise } = fetchMetadata(body);
      await expect(promise).rejects.toThrow("invalid_response");
      expect(fetcher).toHaveBeenCalledTimes(1);
    }
  });

  it("accepts every supported outcome with truthful complete fixed-order coverage", async () => {
    const absent = {
      ...batchProjection,
      state: "absent",
      frontmatter_state: "absent",
      display_title: null,
      title_source: null,
      tags: [],
    };
    const unsupported = {
      ...absent,
      state: "unsupported",
      frontmatter_state: "unsupported",
      heading_state: "unsupported",
      tags: null,
    };
    const truncated = { ...batchProjection, state: "truncated", heading_state: "truncated" };
    const items = [
      batchItem(),
      batchItem({ ref: "empty.md", state: "absent", display_metadata: absent }),
      batchItem({
        ref: "u.md",
        state: "unsupported",
        reason: "unsupported_metadata",
        display_metadata: unsupported,
      }),
      batchItem({
        ref: "t.md",
        state: "truncated",
        reason: "prefix_limit",
        display_metadata: truncated,
      }),
      batchItem({ ref: "c.md", state: "changed", reason: "changed", display_metadata: null }),
      ...[
        "missing_note",
        "private_path",
        "unsafe_path",
        "note_too_large",
        "target_busy",
        "read_failed",
        "invalid_encoding",
      ].map((reason, index) =>
        batchItem({
          ref: `error${index}.md`,
          state: "unreadable",
          reason,
          read_bytes: reason === "read_failed" ? 4096 : reason === "invalid_encoding" ? 2 : 0,
          display_metadata: null,
        }),
      ),
      ...["deadline", "byte_limit", "response_limit"].map((reason, index) =>
        batchItem({
          ref: `unknown${index}.md`,
          state: "unknown",
          reason,
          read_bytes: [0, 4096, 32768][index],
          display_metadata: null,
        }),
      ),
    ];
    const reasons = [
      "deadline",
      "byte_limit",
      "response_limit",
      "unsupported",
      "truncated",
      "changed",
      "unreadable",
    ];
    const body = batchWire({
      items,
      partial: true,
      partial_reasons: reasons,
      read_bytes: items.reduce((sum, item) => sum + item.read_bytes, 0),
      omitted: 3,
    });
    const refs = items.map((item) => item.ref);
    const valid = fetchMetadata(body, refs);
    await expect(valid.promise).resolves.toEqual(body);
    expect(valid.fetcher).toHaveBeenCalledTimes(1);
    for (const changes of [
      { omitted: 2 },
      { partial: false },
      { partial_reasons: reasons.slice(1) },
      { partial_reasons: [...reasons, "unreadable"] },
      { partial_reasons: [...reasons].reverse() },
      { partial_reasons: "unreadable" },
      { read_bytes: Number(body.read_bytes) + 1 },
    ]) {
      const { promise } = fetchMetadata({ ...body, ...changes }, refs);
      await expect(promise).rejects.toThrow("invalid_response");
    }
  });

  it("rejects wrong reason/projection pairs even when coverage and totals look consistent", async () => {
    for (const [state, reason, projection, coverage, omitted] of [
      ["unknown", "deadline", batchProjection, ["deadline"], 1],
      ["unreadable", "read_failed", batchProjection, ["unreadable"], 0],
      ["changed", "changed", batchProjection, ["changed"], 0],
      [
        "unsupported",
        "prefix_limit",
        { ...batchProjection, state: "unsupported", heading_state: "unsupported" },
        ["unsupported"],
        0,
      ],
      [
        "truncated",
        "unsupported_metadata",
        { ...batchProjection, state: "truncated", heading_state: "truncated" },
        ["truncated"],
        0,
      ],
      ["ready", "unsupported_metadata", batchProjection, [], 0],
    ] as const) {
      const { promise } = fetchMetadata(
        batchWire({
          items: [batchItem({ state, reason, display_metadata: projection })],
          partial: coverage.length > 0,
          partial_reasons: coverage,
          omitted,
        }),
      );
      await expect(promise).rejects.toThrow("invalid_response");
    }
  });
});

describe("metadata transport boundaries and cancellation", () => {
  it("accepts 32 refs/512 code points and one MiB charges without granting private-path admission", async () => {
    const refs = [
      `${"🧠".repeat(509)}.MD`,
      ...Array.from({ length: 31 }, (_, i) => `${i}.markdown`),
    ];
    const body = batchWire({
      items: refs.map((ref) => batchItem({ ref, read_bytes: 32768 })),
      read_bytes: 1024 * 1024,
    });
    await expect(fetchMetadata(body, refs).promise).resolves.toEqual(body);
    const privateBody = batchWire({
      items: [
        batchItem({
          ref: ".git/a.md",
          state: "unreadable",
          reason: "private_path",
          read_bytes: 0,
          display_metadata: null,
        }),
      ],
      read_bytes: 0,
      partial: true,
      partial_reasons: ["unreadable"],
    });
    await expect(fetchMetadata(privateBody, [".git/a.md"]).promise).resolves.toEqual(privateBody);
  });

  it("refuses wrong types and C0/C1/DEL/surrogates before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const metadata = createVaultApi("token", fetcher).metadata;
    if (!metadata) throw new Error("metadata_method_missing");
    for (const refs of [
      null,
      "a.md",
      {},
      [1],
      [false],
      ["/a.md"],
      ["a/./b.md"],
      ["a\\b.md"],
      ["C:a.md"],
      ["a.md/"],
      [".markdown"],
      ...[
        ...Array.from({ length: 32 }, (_, i) => i),
        ...Array.from({ length: 33 }, (_, i) => i + 127),
        0xd800,
        0xdbff,
        0xdc00,
        0xdfff,
      ].map((point) => [`x${String.fromCharCode(point)}.md`]),
    ])
      await expect(metadata(ID, refs as unknown as string[])).rejects.toThrow(
        "invalid_metadata_request",
      );
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("caps actual received bytes at 512 KiB and cancels an overflowing reader", async () => {
    const raw = JSON.stringify(batchWire());
    const atCap = new Response(raw + " ".repeat(512 * 1024 - new TextEncoder().encode(raw).length));
    const cancelled = vi.fn();
    const overflow = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(new Uint8Array(512 * 1024 + 1));
        },
        cancel: cancelled,
      }),
    );
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(atCap)
      .mockResolvedValueOnce(overflow);
    const metadata = createVaultApi("token", fetcher).metadata;
    if (!metadata) throw new Error("metadata_method_missing");
    await expect(metadata(ID, ["a.md"])).resolves.toEqual(batchWire());
    await expect(metadata(ID, ["a.md"])).rejects.toThrow("response_too_large");
    expect(cancelled).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("rejects worst-case canonical ASCII expansion even when literal UTF-8 fits the stream cap", async () => {
    const projection = {
      ...batchProjection,
      display_title: "🧠".repeat(512),
      tags: Array.from(
        { length: 16 },
        (_, i) => "🧠".repeat(63) + String.fromCodePoint(0x1f600 + i),
      ),
      type: "🧠".repeat(64),
      status: "🧠".repeat(64),
    };
    const refs = Array.from({ length: 32 }, (_, i) => `${i}-${"🧠".repeat(500)}.md`);
    const body = batchWire({
      items: refs.map((ref) => batchItem({ ref, read_bytes: 32768, display_metadata: projection })),
      read_bytes: 1024 * 1024,
    });
    expect(new TextEncoder().encode(JSON.stringify(body)).length).toBeLessThan(512 * 1024);
    await expect(fetchMetadata(body, refs).promise).rejects.toThrow("invalid_response");
  });

  it("captures request identity before a caller mutates its refs while awaiting the response", async () => {
    let resolve: ((response: Response) => void) | undefined;
    const fetcher = vi.fn<typeof fetch>().mockImplementation(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    const metadata = createVaultApi("token", fetcher).metadata;
    if (!metadata) throw new Error("metadata_method_missing");
    const refs = ["a.md"];
    const promise = metadata(ID, refs);
    refs[0] = "other.md";
    if (!resolve) throw new Error("request_not_started");
    resolve(ok(batchWire()));
    await expect(promise).resolves.toEqual(batchWire());
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({ refs: ["a.md"] });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("reuses pre-abort, pending abort and injected timeout cleanup without retry", async () => {
    vi.useFakeTimers();
    try {
      const pre = new AbortController();
      pre.abort();
      const unused = vi.fn<typeof fetch>();
      const preMethod = createVaultApi("token", unused).metadata;
      if (!preMethod) throw new Error("metadata_method_missing");
      await expect(preMethod(ID, ["a.md"], pre.signal)).rejects.toThrow("cancelled");
      expect(unused).not.toHaveBeenCalled();
      for (const timeout of [false, true]) {
        const controller = new AbortController();
        let forwarded: AbortSignal | null | undefined;
        const fetcher = vi.fn<typeof fetch>().mockImplementation(
          (_url, init) =>
            new Promise<Response>((_resolve, reject) => {
              forwarded = init?.signal;
              forwarded?.addEventListener("abort", () => reject(new Error("aborted")), {
                once: true,
              });
            }),
        );
        const metadata = createVaultApi("token", fetcher).metadata;
        if (!metadata) throw new Error("metadata_method_missing");
        const failure = expect(metadata(ID, ["a.md"], controller.signal)).rejects.toThrow(
          "aborted",
        );
        if (timeout) await vi.advanceTimersByTimeAsync(15000);
        else controller.abort();
        await failure;
        expect(forwarded?.aborted).toBe(true);
        expect(fetcher).toHaveBeenCalledTimes(1);
        expect(vi.getTimerCount()).toBe(0);
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it("carries HTTP error codes through the existing request path and never retries", async () => {
    for (const [status, code] of [
      [403, "forbidden_origin"],
      [404, "unknown_workspace"],
      [429, "overloaded"],
      [503, "root_changed"],
    ] as const) {
      const fetcher = vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          new Response(JSON.stringify({ error: { code, message: "safe" } }), { status }),
        );
      const metadata = createVaultApi("token", fetcher).metadata;
      if (!metadata) throw new Error("metadata_method_missing");
      await expect(metadata(ID, ["a.md"])).rejects.toThrow(code);
      expect(fetcher).toHaveBeenCalledTimes(1);
    }
  });
});

describe("vault overview", () => {
  const note = (changes: Record<string, unknown> = {}) => ({
    ref: "30-knowledge/a.md",
    state: "ready",
    title: "Title",
    type: "note",
    status: "evergreen",
    confidence: "high",
    tags: ["tag"],
    created: "2026-08-18",
    modified: "2026-10-20",
    summary: "A summary.",
    links: null,
    complete: true,
    ...changes,
  });
  const wire = (changes: Record<string, unknown> = {}) => ({
    notes: [note()],
    partial: false,
    partial_reasons: [],
    omitted: 0,
    read_bytes: 4096,
    ...changes,
  });

  it("parses an overview", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(wire()));
    const api = createVaultApi("token", fetcher);
    if (!api.overview) throw new Error("overview_method_missing");
    const controller = new AbortController();
    await expect(api.overview(ID, controller.signal)).resolves.toEqual(wire());
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, init] = fetcher.mock.calls[0] ?? [];
    expect(url).toBe(`/api/v2/vaults/${ID}/overview`);
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer token");
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("maps a 404 to not_found", async () => {
    const api = createVaultApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(new Response("", { status: 404 })),
    );
    if (!api.overview) throw new Error("overview_method_missing");
    await expect(api.overview(ID)).rejects.toThrow("not_found");
  });

  it("accepts null fields, a map's link count, and reasons in their canonical order", async () => {
    const body = wire({
      notes: [
        note({
          title: null,
          type: null,
          status: null,
          confidence: null,
          tags: null,
          created: null,
          modified: null,
          summary: null,
        }),
        note({ ref: "50-maps/m.md", links: 12 }),
      ],
      partial: true,
      partial_reasons: ["deadline", "item_limit", "changed"],
    });
    const api = createVaultApi("token", vi.fn<typeof fetch>().mockResolvedValue(ok(body)));
    if (!api.overview) throw new Error("overview_method_missing");
    await expect(api.overview(ID)).resolves.toEqual(body);
  });

  it("rejects every off-contract overview body, one rule at a time", async () => {
    const bad: unknown[] = [
      null,
      [],
      "ready",
      { ...wire(), extra: true },
      Object.fromEntries(Object.entries(wire()).filter(([key]) => key !== "omitted")),
      { ...wire(), notes: Array.from({ length: 2001 }, () => note()) },
      { ...wire(), notes: [note(), note()] },
      { ...wire(), notes: [{ ...note(), extra: true }] },
      { ...wire(), notes: [{ ...note(), ref: "../x.md" }] },
      { ...wire(), notes: [{ ...note(), ref: "30-knowledge/a.txt" }] },
      { ...wire(), notes: [{ ...note(), state: "bogus" }] },
      { ...wire(), notes: [{ ...note(), title: "x".repeat(513) }] },
      { ...wire(), notes: [{ ...note(), type: "x".repeat(65) }] },
      { ...wire(), notes: [{ ...note(), status: "x".repeat(65) }] },
      { ...wire(), notes: [{ ...note(), confidence: "x".repeat(65) }] },
      { ...wire(), notes: [{ ...note(), tags: Array.from({ length: 17 }, (_, i) => `t${i}`) }] },
      { ...wire(), notes: [{ ...note(), tags: ["same", "same"] }] },
      { ...wire(), notes: [{ ...note(), tags: ["x".repeat(65)] }] },
      { ...wire(), notes: [{ ...note(), created: "2026/08/18" }] },
      { ...wire(), notes: [{ ...note(), modified: 20261020 }] },
      { ...wire(), notes: [{ ...note(), summary: "x".repeat(202) }] },
      { ...wire(), notes: [{ ...note(), summary: "bad\u0000char" }] },
      { ...wire(), notes: [{ ...note(), links: -1 }] },
      { ...wire(), notes: [{ ...note(), links: 257 }] },
      { ...wire(), notes: [{ ...note(), links: 1.5 }] },
      { ...wire(), notes: [{ ...note(), complete: "yes" }] },
      { ...wire(), partial: true, partial_reasons: ["item_limit", "deadline"] },
      { ...wire(), partial: true, partial_reasons: ["deadline", "deadline"] },
      { ...wire(), partial: true, partial_reasons: ["nonsense"] },
      { ...wire(), partial: true, partial_reasons: [] },
      { ...wire(), partial: false, partial_reasons: ["deadline"] },
      { ...wire(), omitted: -1 },
      { ...wire(), omitted: 1.5 },
      { ...wire(), read_bytes: -1 },
      { ...wire(), read_bytes: "4096" },
    ];
    for (const body of bad) {
      const api = createVaultApi("token", vi.fn<typeof fetch>().mockResolvedValue(ok(body)));
      if (!api.overview) throw new Error("overview_method_missing");
      await expect(api.overview(ID)).rejects.toThrow("invalid_response");
    }
  });
});
