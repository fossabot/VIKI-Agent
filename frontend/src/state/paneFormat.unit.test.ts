import { expect, test } from "vitest";
import { MAX_LAYOUT_CHARS, parseLayout, serializeLayout } from "./paneFormat";
import {
  closeTab,
  groups,
  kindOf,
  moveTab,
  openFile,
  openTab,
  type PaneGroup,
  type PaneLayout,
  pinTab,
  singlePane,
  splitFile,
} from "./panes";

function uuid(n: number): string {
  return `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;
}

const TAB_A = uuid(1);
const TAB_B = uuid(2);
const TAB_C = uuid(3);
const TAB_D = uuid(4);

const BASE_ROOT = { group: "g1", tabs: [TAB_A], active: TAB_A };

test("round trips a single group", () => {
  const l = openTab(openTab(singlePane(), TAB_A), TAB_B);
  expect(groups(l)).toHaveLength(1);
  expect(parseLayout(serializeLayout(l))).toEqual(l);
});

test("round trips two groups", () => {
  let l = openTab(singlePane(), TAB_A);
  l = moveTab(l, TAB_B, { group: "g1", zone: "right" });
  expect(groups(l)).toHaveLength(2);
  expect(parseLayout(serializeLayout(l))).toEqual(l);
});

test("round trips four groups, pinned tabs included", () => {
  let l = openTab(singlePane(), TAB_A);
  l = moveTab(l, TAB_B, { group: "g1", zone: "right" });
  l = moveTab(l, TAB_C, { group: "g1", zone: "bottom" });
  l = moveTab(l, TAB_D, { group: "g1", zone: "left" });
  l = pinTab(l, TAB_A, true);
  expect(groups(l)).toHaveLength(4);
  expect(l.pinned).toEqual([TAB_A]);
  expect(parseLayout(serializeLayout(l))).toEqual(l);
});

test("rejects an extra top-level key", () => {
  const text = JSON.stringify({ v: 1, focused: "g1", pinned: [], root: BASE_ROOT, extra: true });
  expect(parseLayout(text)).toBeNull();
});

test("rejects a version other than 1", () => {
  const text = JSON.stringify({ v: 2, focused: "g1", pinned: [], root: BASE_ROOT });
  expect(parseLayout(text)).toBeNull();
});

test("rejects the same tab open in two groups", () => {
  const text = JSON.stringify({
    v: 1,
    focused: "g1",
    pinned: [],
    root: {
      split: "s1",
      dir: "row",
      sizes: [0.5, 0.5],
      children: [
        { group: "g1", tabs: [TAB_A], active: TAB_A },
        { group: "g2", tabs: [TAB_A], active: TAB_A },
      ],
    },
  });
  expect(parseLayout(text)).toBeNull();
});

test("rejects an uppercase UUID", () => {
  const upper = "0123456a-89ab-4cde-8f01-23456789abcd".toUpperCase();
  const text = JSON.stringify({
    v: 1,
    focused: "g1",
    pinned: [],
    root: { group: "g1", tabs: [upper], active: upper },
  });
  expect(parseLayout(text)).toBeNull();
});

test("rejects text over MAX_LAYOUT_CHARS", () => {
  expect(parseLayout("a".repeat(MAX_LAYOUT_CHARS + 1))).toBeNull();
});

test("rejects a non-finite size", () => {
  const text = JSON.stringify({
    v: 1,
    focused: "g1",
    pinned: [],
    root: {
      split: "s1",
      dir: "row",
      sizes: [Number.NaN, 0.5],
      children: [
        { group: "g1", tabs: [TAB_A], active: TAB_A },
        { group: "g2", tabs: [TAB_B], active: TAB_B },
      ],
    },
  });
  expect(parseLayout(text)).toBeNull();
});

test("rejects a tree over 7 nodes", () => {
  const leaf = (id: string, tab: string) => ({ group: id, tabs: [tab], active: tab });
  const text = JSON.stringify({
    v: 1,
    focused: "g1",
    pinned: [],
    root: {
      split: "s1",
      dir: "row",
      sizes: [0.34, 0.33, 0.33],
      children: [
        {
          split: "s2",
          dir: "column",
          sizes: [0.5, 0.5],
          children: [
            {
              split: "s3",
              dir: "row",
              sizes: [0.5, 0.5],
              children: [leaf("g4", TAB_D), leaf("g5", uuid(5))],
            },
            leaf("g3", TAB_C),
          ],
        },
        leaf("g1", TAB_A),
        leaf("g2", TAB_B),
      ],
    },
  });
  expect(parseLayout(text)).toBeNull();
});

test("rejects a non-string input", () => {
  expect(parseLayout(42)).toBeNull();
});

test("serializes 64 tabs and 64 pins within MAX_LAYOUT_CHARS", () => {
  const allTabs = Array.from({ length: 64 }, (_, i) => uuid(i));
  let l = singlePane();
  for (const tab of allTabs) l = openTab(l, tab);
  for (const tab of allTabs) l = pinTab(l, tab, true);
  expect(l.pinned).toHaveLength(64);
  expect(serializeLayout(l).length).toBeLessThanOrEqual(MAX_LAYOUT_CHARS);
});

const FILE_A = { workspaceId: "w1", ref: "notes/a.md" };
const FILE_B = { workspaceId: "w1", ref: "notes/b.md" };

test("a files-less layout still serializes as v1 (no files key)", () => {
  const l = openTab(singlePane(), TAB_A);
  const text = serializeLayout(l);
  expect(JSON.parse(text).v).toBe(1);
  expect("files" in JSON.parse(text)).toBe(false);
});

test("round trips a layout with file tabs as v2", () => {
  let l = openTab(singlePane(), TAB_A);
  l = openFile(l, FILE_A, () => true) as PaneLayout;
  l = openFile(l, FILE_B, () => true) as PaneLayout; // same file group: adds a second file tab
  const text = serializeLayout(l);
  const parsedJson = JSON.parse(text);
  expect(parsedJson.v).toBe(2);
  expect(parsedJson.files).toBeDefined();
  expect(parseLayout(text)).toEqual(l);
});

test("serializeLayout writes files in ascending f-number order regardless of insertion order", () => {
  let l = openFile(singlePane(), FILE_A, () => true) as PaneLayout; // f1
  l = openFile(l, FILE_B, () => true) as PaneLayout; // f2
  const FILE_C = { workspaceId: "w1", ref: "notes/c.md" };
  l = openFile(l, FILE_C, () => true) as PaneLayout; // f3
  l = closeTab(l, "f2"); // frees f2; files' insertion order is now f1, f3
  const FILE_D = { workspaceId: "w1", ref: "notes/d.md" };
  l = openFile(l, FILE_D, () => true) as PaneLayout; // reuses f2 (smallest free)
  expect(Object.keys(l.files)).toEqual(["f1", "f3", "f2"]); // scrambled insertion order, confirmed

  const text = serializeLayout(l);
  const wireFiles = JSON.parse(text).files as Record<string, unknown>;
  expect(Object.keys(wireFiles)).toEqual(["f1", "f2", "f3"]); // written in ascending order regardless
});

test("parseLayout accepts v1 (no files key) exactly as before", () => {
  const text = JSON.stringify({ v: 1, focused: "g1", pinned: [], root: BASE_ROOT });
  expect(parseLayout(text)).not.toBeNull();
});

test("rejects a files key under v1 (extra top-level key)", () => {
  const text = JSON.stringify({
    v: 1,
    focused: "g1",
    pinned: [],
    root: BASE_ROOT,
    files: {},
  });
  expect(parseLayout(text)).toBeNull();
});

test("accepts a v2 payload with an empty files object", () => {
  const text = JSON.stringify({ v: 2, focused: "g1", pinned: [], root: BASE_ROOT, files: {} });
  expect(parseLayout(text)).not.toBeNull();
});

test("rejects a file tab id out of range", () => {
  for (const badId of ["f0", "f65", "f100"]) {
    const text = JSON.stringify({
      v: 2,
      focused: "g1",
      pinned: [],
      root: { group: "g1", tabs: [badId], active: badId },
      files: { [badId]: ["w1", "ref.md"] },
    });
    expect(parseLayout(text)).toBeNull();
  }
});

test("rejects a file tab with no matching files entry", () => {
  const text = JSON.stringify({
    v: 2,
    focused: "g1",
    pinned: [],
    root: { group: "g1", tabs: ["f1"], active: "f1" },
    files: {},
  });
  expect(parseLayout(text)).toBeNull();
});

test("rejects an orphan files entry with no matching tab", () => {
  const text = JSON.stringify({
    v: 2,
    focused: "g1",
    pinned: [],
    root: { group: "g1", tabs: ["f1"], active: "f1" },
    files: { f1: ["w1", "a.md"], f2: ["w1", "b.md"] },
  });
  expect(parseLayout(text)).toBeNull();
});

test("rejects a group mixing a chat tab and a file tab", () => {
  const text = JSON.stringify({
    v: 2,
    focused: "g1",
    pinned: [],
    root: { group: "g1", tabs: [TAB_A, "f1"], active: TAB_A },
    files: { f1: ["w1", "a.md"] },
  });
  expect(parseLayout(text)).toBeNull();
});

// A chat group alongside the file group, needed only so these ref/workspace-id-bound fixtures
// don't trip the separate "a file group needs a chat group too" rule.
const FILE_PLUS_CHAT_ROOT = {
  split: "s1",
  dir: "row",
  sizes: [0.5, 0.5],
  children: [BASE_ROOT, { group: "g2", tabs: ["f1"], active: "f1" }],
};

test("rejects an invalid workspace id (empty, too long, or a control character)", () => {
  const refFor = (workspaceId: string) =>
    JSON.stringify({
      v: 2,
      focused: "g1",
      pinned: [],
      root: FILE_PLUS_CHAT_ROOT,
      files: { f1: [workspaceId, "a.md"] },
    });
  expect(parseLayout(refFor(""))).toBeNull();
  expect(parseLayout(refFor("w".repeat(65)))).toBeNull();
  expect(parseLayout(refFor("w1\u0007"))).toBeNull();
  expect(parseLayout(refFor("w".repeat(64)))).not.toBeNull(); // exactly at the bound: fine
});

test("rejects an invalid ref (empty, too long, a control character, or a leading slash)", () => {
  const layoutWithRef = (ref: string) =>
    JSON.stringify({
      v: 2,
      focused: "g1",
      pinned: [],
      root: FILE_PLUS_CHAT_ROOT,
      files: { f1: ["w1", ref] },
    });
  expect(parseLayout(layoutWithRef(""))).toBeNull();
  expect(parseLayout(layoutWithRef("x".repeat(1025)))).toBeNull();
  expect(parseLayout(layoutWithRef("a\u0000b"))).toBeNull();
  expect(parseLayout(layoutWithRef("/abs/path.md"))).toBeNull();
  expect(parseLayout(layoutWithRef("\\abs\\path.md"))).toBeNull();
  expect(parseLayout(layoutWithRef("x".repeat(1024)))).not.toBeNull(); // exactly at the bound
});

test("serializeLayout drops inactive file tabs from the last group first to stay within budget", () => {
  // group A (tree-first): small, one inactive tab — must survive untouched. The chat-group rule
  // keeps the bare singlePane's original (empty) chat group alongside it; look group A up by its
  // tab rather than assuming it is groups(l)[0].
  let l = openFile(singlePane(), FILE_A, () => true) as PaneLayout;
  l = openFile(l, FILE_B, () => true) as PaneLayout; // group A: [f1 inactive, f2 active]
  const groupA = groups(l).find((g) => g.tabs.includes("f1")) as PaneGroup;
  expect(groupA.tabs).toEqual(["f1", "f2"]);

  // group B (tree-last): bulked up with many large, inactive file tabs.
  l = splitFile(l, "f1", "right") as PaneLayout; // group B: [copy of f1], focused
  for (let i = 0; i < 20; i += 1) {
    l = openFile(
      l,
      { workspaceId: "w1", ref: `large-${i}-${"x".repeat(1000)}` },
      () => true,
    ) as PaneLayout;
  }
  const groupB = groups(l).find((g) => g.id !== groupA.id && kindOf(g) === "file") as PaneGroup;
  const groupBActive = groupB.active as string;

  const text = serializeLayout(l);
  expect(text.length).toBeLessThanOrEqual(MAX_LAYOUT_CHARS);
  const parsed = parseLayout(text);
  expect(parsed).not.toBeNull();
  const after = parsed as PaneLayout;

  expect(after.files.f1).toEqual(FILE_A); // group A's (tree-first) inactive tab survived
  expect(after.files[groupBActive]).toBeDefined(); // the active tab is never dropped
  const afterGroupB = groups(after).find((g) => g.id === groupB.id) as PaneGroup;
  expect(afterGroupB.tabs.length).toBeLessThan(groupB.tabs.length); // something was dropped
});

test("splitFile's eviction keeps a 64-tab layout parseable; a bad ref never enters files", () => {
  let l = openTab(singlePane(), uuid(100));
  for (let i = 101; i <= 162; i += 1) l = openTab(l, uuid(i)); // chat group: 63 tabs
  l = openFile(l, FILE_A, () => true) as PaneLayout; // + file group: f1 (64 tabs total, at the cap)

  l = splitFile(l, "f1", "right"); // copies f1 into f2: one tab over the cap
  const all = groups(l).flatMap((g) => g.tabs);
  expect(all).toHaveLength(64); // evicted back down to the cap, never left over it

  const parsed = parseLayout(serializeLayout(l));
  expect(parsed).not.toBeNull();
  expect(groups(parsed as PaneLayout).flatMap((g) => g.tabs)).toHaveLength(64);

  // An over-long, control-character or leading-slash ref is refused before it ever reaches `files`.
  expect(openFile(l, { workspaceId: "w1", ref: "x".repeat(1025) }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w1", ref: "a\u0000b" }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w1", ref: "/abs.md" }, () => true)).toBeNull();
  expect(Object.keys(l.files)).toEqual(["f1", "f2"]); // untouched by the three refusals above
});
