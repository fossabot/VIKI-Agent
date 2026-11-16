/**
 * Pins the overview page's derivations: day grouping,
 * growth buckets, top tags, maps-by-links and inbox age text, all against one fixed "now".
 */
import { expect, test } from "vitest";
import {
  ageText,
  datedCount,
  dayNumber,
  FOLDER_SECTIONS,
  fallbackTitle,
  growthOf,
  leadOf,
  mapsOf,
  otherFolders,
  recentGroups,
  rediscover,
  type StatsNote,
  sectionOf,
  todayNumber,
  topTags,
} from "./vaultOverviewStats";

// 2026-10-20, local noon: noon keeps every date below clear of a midnight/DST edge. Also a
// Tuesday, so its own Monday is 2026-10-19 and the week before starts 2026-10-12.
const NOW = new Date(2026, 9, 20, 12);
const TODAY = todayNumber(NOW);

function localIso(year: number, month1: number, day: number, hour = 0, minute = 0): string {
  return new Date(year, month1 - 1, day, hour, minute).toISOString();
}

function note(overrides: Partial<StatsNote> & { ref: string }): StatsNote {
  return {
    state: "ready",
    title: null,
    type: "note",
    status: null,
    confidence: null,
    tags: null,
    created: null,
    modified: null,
    summary: null,
    links: null,
    complete: true,
    ...overrides,
  };
}

test("sectionOf reads the vault's three review folders and nothing else", () => {
  expect(sectionOf("30-knowledge/claim.md")).toBe("Knowledge");
  expect(sectionOf("40-sources/paper.md")).toBe("Source");
  expect(sectionOf("50-maps/Second Brain MOC.md")).toBe("Map");
  expect(sectionOf("00-inbox/x.md")).toBeNull();
  expect(sectionOf("30-knowledge-extra/x.md")).toBeNull(); // a prefix, not the folder itself
});

test("fallbackTitle falls back to the stem for an ordinary note", () => {
  expect(fallbackTitle("30-knowledge/deny-rules-hold.md")).toBe("deny-rules-hold");
});

test("fallbackTitle names SKILL/README/index by their parent directory, any case", () => {
  expect(fallbackTitle(".claude/skills/research/SKILL.md")).toBe("research / SKILL.md");
  expect(fallbackTitle("docs/guide/readme.MD")).toBe("guide / readme.MD");
  expect(fallbackTitle("a/b/Index.md")).toBe("b / Index.md");
});

test("fallbackTitle has no parent to borrow at the vault root", () => {
  expect(fallbackTitle("README.md")).toBe("README.md");
});

test("folder sections use exact top-level names and stable identities", () => {
  expect(Object.keys(FOLDER_SECTIONS)).toEqual([
    "00-inbox",
    "10-projects",
    "20-areas",
    "30-knowledge",
    "40-sources",
    "50-maps",
    "60-calendar",
    "70-people",
    "kanban",
  ]);
  expect(FOLDER_SECTIONS["30-knowledge"]).toEqual({ key: "knowledge", defaultType: "note" });
  expect(FOLDER_SECTIONS["40-sources"]).toEqual({ key: "sources", defaultType: "source" });
  expect(FOLDER_SECTIONS["50-maps"]).toEqual({ key: "maps", defaultType: "moc" });
});

test("otherFolders groups exact unknown folders, excludes private and archive paths, and marks lower bounds", () => {
  const rows = [
    "root.md",
    "Misc/a.md",
    "Misc/b.md",
    "Misc/undated.md",
    "日本語/案内.md",
    "Archive/a.md",
    "archive/a.md",
    "90-archive/a.md",
    "bot-memory/a.md",
    ".private/a.md",
    "kanban/a.md",
  ].map((ref) => ({
    ref,
    title: ref,
    bytes: 1,
    modified: ref === "Misc/undated.md" ? undefined : 100,
  }));
  expect(otherFolders(rows, true)).toEqual([
    { folder: "Archive", count: 1, newest: 100, partial: true },
    { folder: "Misc", count: 3, newest: 100, partial: true },
    { folder: "日本語", count: 1, newest: 100, partial: true },
  ]);
  expect(otherFolders(rows, false)[0]?.partial).toBe(false);
});

test("leadOf picks newest dated summary then exact ref", () => {
  const rows = [
    note({ ref: "30-knowledge/z.md", title: "Same", summary: "z", modified: "2026-10-19" }),
    note({ ref: "30-knowledge/b.md", title: "Same", summary: "b", modified: "2026-10-20" }),
    note({ ref: "30-knowledge/a.md", title: "Same", summary: "a", created: "2026-10-20" }),
    note({ ref: "30-knowledge/empty.md", summary: "  ", modified: "2026-10-21" }),
  ];
  expect(leadOf(rows)?.ref).toBe("30-knowledge/a.md");
});

test("rediscover uses positive listing times, the inclusive 30-day boundary, and ref ties", () => {
  const boundary = NOW.getTime() / 1000 - 30 * 86_400;
  const refs = [
    "30-knowledge/z.md",
    "30-knowledge/a.md",
    "30-knowledge/new.md",
    "30-knowledge/zero.md",
    "40-sources/old.md",
    "30-knowledge/missing.md",
  ];
  const fileTimes = new Map([
    ["30-knowledge/z.md", boundary],
    ["30-knowledge/a.md", boundary],
    ["30-knowledge/new.md", boundary + 1],
    ["30-knowledge/zero.md", 0],
    ["40-sources/old.md", boundary - 1],
  ]);
  const listing = refs.map((ref) => ({
    ref,
    title: ref,
    bytes: 1,
    modified: ref === "30-knowledge/missing.md" ? undefined : (fileTimes.get(ref) ?? 0),
  }));
  expect(rediscover(listing, NOW).map((row) => row.ref)).toEqual([refs[1], refs[0]]);
  expect(rediscover(listing, NOW, 0)).toEqual([]);
});

test("dayNumber and todayNumber place the same calendar day in the same slot", () => {
  expect(dayNumber("2026-10-20")).toBe(TODAY);
  expect(dayNumber("2026-10-19")).toBe(TODAY - 1);
});

test("groups by day", () => {
  const today = note({ ref: "30-knowledge/today.md", created: "2026-10-20" });
  const yesterday = note({ ref: "30-knowledge/yesterday.md", created: "2026-10-19" });
  const older = note({ ref: "30-knowledge/older.md", created: "2026-10-05" });
  const olderLabel = new Date(dayNumber("2026-10-05") * 86_400_000).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });

  const groups = recentGroups([older, today, yesterday], TODAY, 10);

  expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday", olderLabel]);
  expect(groups.map((g) => g.items.map((item) => item.row.ref))).toEqual([
    ["30-knowledge/today.md"],
    ["30-knowledge/yesterday.md"],
    ["30-knowledge/older.md"],
  ]);
});

test("recentGroups picks the later date for a row's day, and says added only when created matches it", () => {
  const createdOnly = note({ ref: "30-knowledge/a.md", created: "2026-10-20" });
  const editedLater = note({
    ref: "30-knowledge/b.md",
    created: "2026-10-05",
    modified: "2026-10-20",
  });
  const groups = recentGroups([createdOnly, editedLater], TODAY, 10);

  expect(groups).toHaveLength(1);
  expect(groups[0]?.label).toBe("Today");
  const verbs = new Map(groups[0]?.items.map((item) => [item.row.ref, item.verb]));
  expect(verbs.get("30-knowledge/a.md")).toBe("added");
  expect(verbs.get("30-knowledge/b.md")).toBe("updated");
});

test("recentGroups breaks a same-day tie by title, and truncates before grouping", () => {
  const b = note({ ref: "30-knowledge/b.md", title: "Banana", created: "2026-10-20" });
  const a = note({ ref: "30-knowledge/a.md", title: "Apple", created: "2026-10-20" });
  const c = note({ ref: "30-knowledge/c.md", title: "Cherry", created: "2026-10-19" });

  // limit 2 keeps the two Today notes (day desc beats Cherry's Yesterday); Apple before Banana.
  const groups = recentGroups([b, a, c], TODAY, 2);

  expect(groups).toEqual([
    {
      day: TODAY,
      label: "Today",
      items: [
        { row: a, verb: "added" },
        { row: b, verb: "added" },
      ],
    },
  ]);
});

test("datedCount counts rows with a created or modified date, not every row", () => {
  const created = note({ ref: "30-knowledge/a.md", created: "2026-10-01" });
  const modifiedOnly = note({ ref: "30-knowledge/b.md", modified: "2026-10-01" });
  const undated = note({ ref: "30-knowledge/c.md" });
  expect(datedCount([created, modifiedOnly, undated])).toBe(2);
});

test("growthOf buckets knowledge status, telling unreadable apart from no status", () => {
  const evergreen = note({ ref: "30-knowledge/a.md", status: "evergreen" });
  const draft = note({ ref: "30-knowledge/b.md", status: "draft" });
  const none = note({ ref: "30-knowledge/c.md", status: null, state: "ready" });
  const unreadable = note({ ref: "30-knowledge/d.md", status: null, state: "unsupported" });
  // Only the heading/summary was truncated; frontmatter (and so status) still read fine.
  const trimmedButReadable = note({
    ref: "30-knowledge/e.md",
    status: "evergreen",
    state: "truncated",
  });
  const odd = note({ ref: "30-knowledge/f.md", status: "seed" }); // not a knowledge status value

  const growth = growthOf([evergreen, draft, none, unreadable, trimmedButReadable, odd], TODAY);

  expect(growth.knowledge.total).toBe(6);
  expect(growth.knowledge.status).toEqual({
    evergreen: 2,
    draft: 1,
    none: 1,
    unreadable: 1,
    other: 1,
  });
});

test("growthOf buckets knowledge confidence, with a missing value counting as none", () => {
  const high = note({ ref: "30-knowledge/a.md", confidence: "high" });
  const medium = note({ ref: "30-knowledge/b.md", confidence: "medium" });
  const low = note({ ref: "30-knowledge/c.md", confidence: "low" });
  const none = note({ ref: "30-knowledge/d.md", confidence: null });
  const odd = note({ ref: "30-knowledge/e.md", confidence: "mixed" });

  const growth = growthOf([high, medium, low, none, odd], TODAY);

  expect(growth.knowledge.confidence).toEqual({ high: 1, medium: 1, low: 1, none: 1, other: 1 });
});

test("growthOf buckets sources by processed/unread, counts maps, and ignores other sections", () => {
  const processed = note({ ref: "40-sources/a.md", status: "processed" });
  const unread = note({ ref: "40-sources/b.md", status: "unread" });
  const odd = note({ ref: "40-sources/c.md", status: null });
  const map = note({ ref: "50-maps/Home.md" });
  const outOfScope = note({ ref: "00-inbox/x.md" }); // not one of the three review sections

  const growth = growthOf([processed, unread, odd, map, outOfScope], TODAY);

  expect(growth.sources).toEqual({ total: 3, processed: 1, unread: 1, other: 1 });
  expect(growth.maps).toEqual({ total: 1 });
  expect(growth.knowledge.total).toBe(0);
});

test("growthOf builds 12 Monday-start weeks oldest first, counting created across all rows", () => {
  const thisWeek = note({ ref: "30-knowledge/a.md", created: "2026-10-19" }); // this week's Monday
  const lastWeek = note({ ref: "40-sources/b.md", created: "2026-10-12" }); // the Monday before it

  const growth = growthOf([thisWeek, lastWeek], TODAY);

  expect(growth.weeks).toHaveLength(12);
  expect(growth.weeks[11]?.count).toBe(1); // the current week: last in the oldest-first list
  expect(growth.weeks[10]?.count).toBe(1); // the week before it
  expect(growth.weeks.slice(0, 10).every((week) => week.count === 0)).toBe(true);
  for (let i = 1; i < growth.weeks.length; i++) {
    expect(growth.weeks[i]?.start).toBe((growth.weeks[i - 1]?.start ?? 0) + 7);
  }
  expect(growth.lastFourWeeks).toBe(2);
});

test("topTags sorts by count desc then tag asc, and truncates to n", () => {
  const rows = [
    note({ ref: "30-knowledge/a.md", tags: ["pkm", "ai"] }),
    note({ ref: "30-knowledge/b.md", tags: ["ai"] }),
    note({ ref: "30-knowledge/c.md", tags: ["zen"] }),
    note({ ref: "30-knowledge/d.md", tags: null }),
  ];

  expect(topTags(rows)).toEqual([
    { tag: "ai", count: 2 },
    { tag: "pkm", count: 1 },
    { tag: "zen", count: 1 },
  ]);
  expect(topTags(rows, 1)).toEqual([{ tag: "ai", count: 2 }]);
});

test("mapsOf keeps only Map rows, sorted by links desc (null as 0) then title", () => {
  const unlinked = note({ ref: "50-maps/Z.md", title: "Z Map", links: null });
  const popular = note({ ref: "50-maps/A.md", title: "A Map", links: 5 });
  const tiedLower = note({ ref: "50-maps/C.md", title: "Tied A", links: 2 });
  const tiedHigher = note({ ref: "50-maps/B.md", title: "Tied B", links: 2 });
  const notAMap = note({ ref: "30-knowledge/x.md", links: 99 });

  const maps = mapsOf([unlinked, popular, tiedHigher, tiedLower, notAMap]);

  expect(maps.map((row) => row.ref)).toEqual([
    "50-maps/A.md",
    "50-maps/C.md", // "Tied A" sorts before "Tied B" on the links tie
    "50-maps/B.md",
    "50-maps/Z.md",
  ]);
});

test("ageText reports today/yesterday/days/weeks from captured, falling back to modified", () => {
  expect(ageText(localIso(2026, 10, 20, 9), null, NOW)).toBe("today");
  expect(ageText(localIso(2026, 10, 19, 9), null, NOW)).toBe("yesterday");
  expect(ageText(localIso(2026, 10, 13, 12), null, NOW)).toBe("7 days ago");
  expect(ageText(localIso(2026, 10, 6, 12), null, NOW)).toBe("2 weeks ago");
  expect(ageText(null, localIso(2026, 10, 19, 9), NOW)).toBe("yesterday"); // captured absent
  expect(ageText("not-a-date", localIso(2026, 10, 19, 9), NOW)).toBe("yesterday"); // unparseable
  expect(ageText(null, null, NOW)).toBe(""); // neither parses
  expect(ageText(localIso(2026, 10, 25, 9), null, NOW)).toBe("today"); // future date clamps
});
