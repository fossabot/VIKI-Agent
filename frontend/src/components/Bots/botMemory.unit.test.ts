import { describe, expect, it, vi } from "vitest";
import type { VaultApi } from "../../state/vaultApi";
import { botMemoryCard, listBotMemory } from "./botMemory";

const WORKSPACE = "11111111-1111-4111-8111-111111111111";
const BOT = "22222222-2222-4222-8222-222222222222";

function entry(ref: string, modified = 0, title = ref): Record<string, unknown> {
  return { ref, title, modified, bytes: 12 };
}

function metadata(
  ref: string,
  state: "absent" | "unsupported" | "ready" = "absent",
): Record<string, unknown> {
  return {
    ref,
    state,
    reason: state === "unsupported" ? "unsupported_metadata" : null,
    read_bytes: 0,
    display_metadata: {
      state,
      frontmatter_state: state,
      heading_state: state,
      display_title: null,
      title_source: null,
      tags: state === "absent" ? [] : null,
      type: null,
      status: null,
    },
  };
}

function api(entries: Record<string, unknown>[], batch?: Record<string, unknown>[]) {
  const result = {
    entries: vi.fn(async () => ({ entries, partial: false, partial_reasons: [], omitted: 0 })),
    metadata: vi.fn(async (_workspace: string, refs: readonly string[]) => ({
      items: batch
        ? refs.map((ref) => batch.find((item) => item.ref === ref) ?? metadata(ref))
        : refs.map((ref) => metadata(ref)),
      partial: false,
      partial_reasons: [],
      read_bytes: 0,
      omitted: 0,
    })),
  };
  return result as unknown as Pick<VaultApi, "entries" | "metadata"> & typeof result;
}

describe("listBotMemory app-kept mode", () => {
  const AREA = `/data/bot-memory/${WORKSPACE}`;

  it("scans the Bot prefix of the app area and returns its folder", async () => {
    const vault = api([]);
    (vault.entries as ReturnType<typeof vi.fn>).mockImplementation(async () => ({
      entries: [
        entry(`${BOT}/MEMORY.md`, 1),
        entry(`${BOT}/topic.md`, 5),
        entry(`33333333-3333-4333-8333-333333333333/other.md`, 9),
      ],
      partial: false,
      partial_reasons: [],
      omitted: 0,
      folder: AREA,
    }));

    const result = await listBotMemory(vault, WORKSPACE, BOT, undefined, () => true, "app");

    expect(result.rows.map((row) => row.ref)).toEqual([`${BOT}/MEMORY.md`, `${BOT}/topic.md`]);
    expect(result.folder).toBe(AREA);
  });

  it("keeps vault mode on the bot-memory prefix with no folder", async () => {
    const result = await listBotMemory(api([entry(`bot-memory/${BOT}/a.md`)]), WORKSPACE, BOT);
    expect(result.folder).toBeNull();
    expect(result.rows[0]?.ref).toBe(`bot-memory/${BOT}/a.md`);
  });

  it.each([
    ["relative", "data/bot-memory"],
    ["empty", ""],
    ["a control character", "/data/\nx"],
    ["not a string", 5],
  ])("reads no folder from %s", async (_name, folder) => {
    const vault = api([]);
    (vault.entries as ReturnType<typeof vi.fn>).mockImplementation(async () => ({
      entries: [],
      partial: false,
      partial_reasons: [],
      omitted: 0,
      folder,
    }));
    const result = await listBotMemory(vault, WORKSPACE, BOT, undefined, () => true, "app");
    expect(result.folder).toBeNull();
  });

  it("accepts a Windows drive folder", async () => {
    const vault = api([]);
    (vault.entries as ReturnType<typeof vi.fn>).mockImplementation(async () => ({
      entries: [],
      partial: false,
      partial_reasons: [],
      omitted: 0,
      folder: "C:\\data\\bot-memory\\x",
    }));
    const result = await listBotMemory(vault, WORKSPACE, BOT, undefined, () => true, "app");
    expect(result.folder).toBe("C:\\data\\bot-memory\\x");
  });
});

describe("listBotMemory", () => {
  it("scans only selected Bot folder, orders index then newest, and enriches at most 32 refs once", async () => {
    const rows = Array.from({ length: 35 }, (_, index) =>
      entry(`bot-memory/${BOT}/topic-${String(index).padStart(2, "0")}.md`, index),
    );
    rows.push(entry(`bot-memory/${BOT}/MEMORY.md`, 1));
    rows.push(entry(`bot-memory/33333333-3333-4333-8333-333333333333/other.md`, 999));
    const vault = api(rows);

    const result = await listBotMemory(vault, WORKSPACE, BOT);

    expect(vault.entries).toHaveBeenCalledTimes(1);
    expect(vault.entries).toHaveBeenCalledWith(WORKSPACE, undefined, undefined);
    expect(vault.metadata).toHaveBeenCalledTimes(1);
    expect(vault.metadata.mock.calls[0]?.[1]).toHaveLength(32);
    expect(result.rows[0]?.ref).toBe(`bot-memory/${BOT}/MEMORY.md`);
    expect(result.rows[1]?.ref).toBe(`bot-memory/${BOT}/topic-34.md`);
    expect(result.rows.at(-1)?.details).toBe("notRead");
    expect(result.rows).toHaveLength(36);
  });

  it("uses supported title, then non-generic legacy title, then path fallback", async () => {
    const refs = [
      `bot-memory/${BOT}/authored.md`,
      `bot-memory/${BOT}/legacy.md`,
      `bot-memory/${BOT}/SKILL.md`,
    ];
    const vault = api(
      refs.map((ref, index) => entry(ref, 3 - index, ref.endsWith("legacy.md") ? "Legacy" : "")),
      [
        {
          ...metadata(refs[0] ?? ""),
          state: "ready",
          reason: null,
          read_bytes: 40,
          display_metadata: {
            state: "ready",
            frontmatter_state: "ready",
            heading_state: "absent",
            display_title: "Authored",
            title_source: "title",
            tags: [],
            type: null,
            status: null,
          },
        },
        metadata(refs[1] ?? ""),
        metadata(refs[2] ?? "", "unsupported"),
      ],
    );

    const result = await listBotMemory(vault, WORKSPACE, BOT);

    expect(result.rows.map(({ title }) => title)).toEqual([
      "Authored",
      "Legacy",
      `${BOT} / SKILL.md`,
    ]);
    expect(result.rows.map(({ ref }) => ref)).toEqual(refs);
    expect(result.rows[0]?.details).toBeNull();
    expect(result.rows[2]?.details).toBe("unsupported");
  });

  it("pins only the Bot folder's root MEMORY.md before newest topic files", async () => {
    const root = `bot-memory/${BOT}/MEMORY.md`;
    const nested = `bot-memory/${BOT}/topics/MEMORY.md`;
    const newest = `bot-memory/${BOT}/topic.md`;
    const result = await listBotMemory(
      api([entry(root, 1), entry(nested, 100), entry(newest, 200)]),
      WORKSPACE,
      BOT,
    );

    expect(result.rows.map(({ ref }) => ref)).toEqual([root, newest, nested]);
  });

  it("sorts nested MEMORY.md as a topic when root MEMORY.md is absent", async () => {
    const nested = `bot-memory/${BOT}/topics/MEMORY.md`;
    const older = `bot-memory/${BOT}/older.md`;
    const result = await listBotMemory(api([entry(older, 5), entry(nested, 100)]), WORKSPACE, BOT);

    expect(result.rows.map(({ ref }) => ref)).toEqual([nested, older]);
  });

  it.each([3, 36])(
    "marks all %i rows details-not-read when metadata API is unavailable",
    async (count) => {
      const refs = Array.from(
        { length: count },
        (_, index) => `bot-memory/${BOT}/topic-${index}.md`,
      );
      const noMetadata = {
        entries: vi.fn(async () => ({
          entries: refs.map((ref) => entry(ref)),
          partial: false,
          partial_reasons: [],
          omitted: 0,
        })),
      } as unknown as Pick<VaultApi, "entries" | "metadata">;

      const result = await listBotMemory(noMetadata, WORKSPACE, BOT);

      expect(result.rows.map(({ details }) => details)).toEqual(Array(count).fill("notRead"));
    },
  );

  it("rejects malformed selected rows and passes on the metadata client's invalid response", async () => {
    const malformedEntry = api([
      entry(`bot-memory/${BOT}/topic.md`),
      entry(`bot-memory/${BOT}/bad.md`, Number.NaN),
    ]);
    await expect(listBotMemory(malformedEntry, WORKSPACE, BOT)).rejects.toThrow("invalid_response");

    // VaultApi.metadata validates each batch against its requested refs and throws this.
    const rejected = api([entry(`bot-memory/${BOT}/topic.md`)]);
    rejected.metadata = vi.fn(async () => {
      throw new Error("invalid_response");
    }) as unknown as typeof rejected.metadata;
    await expect(listBotMemory(rejected, WORKSPACE, BOT)).rejects.toThrow("invalid_response");
  });

  it.each([
    ["absent", false],
    ["unsupported", true],
    ["truncated", true],
    ["changed", true],
    ["unreadable", true],
    ["unknown", true],
  ] as const)("maps a %s metadata state to its own row detail", async (state, partial) => {
    const ref = `bot-memory/${BOT}/topic.md`;
    const projected = state === "absent" || state === "unsupported" || state === "truncated";
    const vault = api([entry(ref)]);
    vault.metadata = vi.fn(async () => ({
      items: [
        {
          ref,
          state,
          reason: {
            absent: null,
            unsupported: "unsupported_metadata",
            truncated: "prefix_limit",
            changed: "changed",
            unreadable: "read_failed",
            unknown: "deadline",
          }[state],
          read_bytes: 0,
          display_metadata: projected
            ? {
                state,
                frontmatter_state: state,
                heading_state: state,
                display_title: null,
                title_source: null,
                tags: state === "absent" ? [] : null,
                type: null,
                status: null,
              }
            : null,
        },
      ],
      partial,
      partial_reasons: partial ? [state] : [],
      read_bytes: 0,
      omitted: 0,
    })) as unknown as typeof vault.metadata;

    const result = await listBotMemory(vault, WORKSPACE, BOT);

    expect(result.rows.map(({ details }) => details)).toEqual([state]);
    expect(result.metadataPartial).toBe(partial);
  });

  it("marks every enriched row unknown and the listing partial when the metadata call fails", async () => {
    const vault = api([entry(`bot-memory/${BOT}/one.md`, 2), entry(`bot-memory/${BOT}/two.md`, 1)]);
    vault.metadata = vi.fn(async () => {
      throw new Error("offline");
    }) as unknown as typeof vault.metadata;

    const result = await listBotMemory(vault, WORKSPACE, BOT);

    expect(result.rows.map(({ details }) => details)).toEqual(["unknown", "unknown"]);
    expect(result.metadataPartial).toBe(true);
  });

  it("keeps failed scan distinct from an empty complete scan", async () => {
    const failed = api([]);
    failed.entries = vi.fn(async () => {
      throw new Error("unavailable");
    }) as typeof failed.entries;
    await expect(listBotMemory(failed, WORKSPACE, BOT)).rejects.toThrow("unavailable");

    const empty = await listBotMemory(api([]), WORKSPACE, BOT);
    expect(empty.rows).toEqual([]);
    expect(empty.partial).toBe(false);
  });

  it("limits preview to five rows and reports remaining count", async () => {
    const vault = api(
      Array.from({ length: 7 }, (_, index) =>
        entry(`bot-memory/${BOT}/topic-${index}.md`, 7 - index),
      ),
    );
    const listing = await listBotMemory(vault, WORKSPACE, BOT);

    expect(botMemoryCard(listing)).toEqual({ rows: listing.rows.slice(0, 5), remaining: 2 });
  });
});
