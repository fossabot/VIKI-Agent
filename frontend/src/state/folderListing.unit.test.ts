import { describe, expect, it } from "vitest";
import { folderListing } from "./folderListing";

describe("folderListing", () => {
  it("matches whole prefix segments and optionally direct children", () => {
    const raw = {
      entries: [
        { ref: "bot-memory/id/MEMORY.md", bytes: 12, modified: 1 },
        { ref: "bot-memory/id/topics/a.md", bytes: 13, modified: 2 },
        { ref: "bot-memory/id-other/MEMORY.md", bytes: 14, modified: 3 },
        { ref: "Bot-memory/id/MEMORY.md", bytes: 15, modified: 4 },
        { ref: "bot-memory/other/MEMORY.md", bytes: 16, modified: 5 },
      ],
      partial: true,
      partial_reasons: ["depth_limit"],
      omitted: 2,
    };

    expect(folderListing(raw, "bot-memory/id", false)).toEqual({
      entries: raw.entries.slice(0, 2),
      partial: true,
      partial_reasons: ["depth_limit"],
      omitted: 2,
    });
    expect(folderListing(raw, "bot-memory/id", true)).toEqual({
      entries: [raw.entries[0]],
      partial: true,
      partial_reasons: ["depth_limit"],
      omitted: 2,
    });
  });

  it("accepts optional selected bytes and modified but ignores malformed off-folder metadata", () => {
    expect(
      folderListing(
        {
          entries: [
            { ref: "kanban/a.md" },
            { ref: "kanban/b.md", modified: 1.5 },
            { ref: "kanban/c.md", bytes: "bad" },
            { ref: "other/c.md", modified: "bad", bytes: "bad" },
          ],
          partial: false,
          partial_reasons: [],
          omitted: 0,
        },
        "kanban",
        true,
      ).entries,
    ).toEqual([
      { ref: "kanban/a.md" },
      { ref: "kanban/b.md", modified: 1.5 },
      { ref: "kanban/c.md", bytes: "bad" },
    ]);
  });

  it("rejects malformed envelopes, nonobject rows, selected rows and duplicates", () => {
    const base = {
      entries: [{ ref: "kanban/a.md" }],
      partial: false,
      partial_reasons: [],
      omitted: 0,
    };
    const invalid = [
      { ...base, partial: 1 },
      { ...base, partial_reasons: [1] },
      { ...base, omitted: -1 },
      { ...base, entries: [{ ref: "else/a.md" }, null] },
      { ...base, entries: [{ ref: "kanban/" }] },
      { ...base, entries: [{ ref: "kanban/a.md", modified: "bad" }] },
      { ...base, entries: [{ ref: "kanban/a.md" }, { ref: "kanban/a.md" }] },
    ];
    for (const raw of invalid)
      expect(() => folderListing(raw, "kanban", true)).toThrow("invalid_response");
  });
});
