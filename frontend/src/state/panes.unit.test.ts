import { expect, test } from "vitest";
import { parseLayout, serializeLayout } from "./paneFormat";
import type { PaneGroup, PaneLayout, PaneSplit } from "./panes";
import {
  clearActive,
  closeTab,
  FILE_PANE_MIN,
  focusedGroup,
  focusGroup,
  groupOf,
  groups,
  kindOf,
  MAX_PANES,
  MAX_TABS,
  minSize,
  moveTab,
  normalize,
  openFile,
  openTab,
  PANE_MIN,
  pinTab,
  resize,
  retainTabs,
  singlePane,
  splitFile,
} from "./panes";

const FILE_A = { workspaceId: "w1", ref: "notes/a.md" };
const FILE_B = { workspaceId: "w1", ref: "notes/b.md" };

test("singlePane starts with one empty focused group and no pins", () => {
  const l = singlePane();
  expect(groups(l)).toEqual([{ kind: "group", id: "g1", tabs: [], active: null }]);
  expect(l.focused).toBe("g1");
  expect(l.pinned).toEqual([]);
  expect(focusedGroup(l)).toEqual(groups(l)[0]);
});

test("openTab appends a new tab to the focused group and activates it", () => {
  const l = openTab(singlePane(), "t1");
  const g = groups(l)[0] as PaneGroup;
  expect(g.tabs).toEqual(["t1"]);
  expect(g.active).toBe("t1");
  expect(l.focused).toBe(g.id);
});

test("openTab on an already-open tab activates it without duplicating", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  const before = l;
  l = openTab(l, "t1");
  const g = groups(l)[0] as PaneGroup;
  expect(g.tabs).toEqual(["t1", "t2"]);
  expect(g.active).toBe("t1");
  expect(l).not.toBe(before);
});

test("groupOf finds a tab's group and is undefined for an unknown tab", () => {
  const l = openTab(singlePane(), "t1");
  expect(groupOf(l, "t1")).toEqual(groups(l)[0]);
  expect(groupOf(l, "nope")).toBeUndefined();
});

test("focusGroup activates the group's last tab when it had none active", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  l = clearActive(l);
  const gid = (groups(l)[0] as PaneGroup).id;
  l = focusGroup(l, gid);
  const g = groups(l)[0] as PaneGroup;
  expect(g.active).toBe("t2");
  expect(l.focused).toBe(gid);
});

test("focusGroup on an unknown id is a no-op", () => {
  const l = openTab(singlePane(), "t1");
  expect(focusGroup(l, "nope")).toBe(l);
});

test("clearActive nulls the focused group's active tab and is then idempotent", () => {
  const l = openTab(singlePane(), "t1");
  const cleared = clearActive(l);
  expect((groups(cleared)[0] as PaneGroup).active).toBeNull();
  expect(clearActive(cleared)).toBe(cleared);
});

test("closeTab moves active to the tab now at its index, else the one before", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  l = openTab(l, "t3"); // [t1,t2,t3], active t3
  l = openTab(l, "t2"); // reactivate the middle tab, order unchanged
  l = closeTab(l, "t2"); // removes the active middle tab: the one now at its slot (t3) activates
  let g = groups(l)[0] as PaneGroup;
  expect(g.tabs).toEqual(["t1", "t3"]);
  expect(g.active).toBe("t3");

  l = closeTab(l, "t3"); // removes the active last tab: falls back to the one before
  g = groups(l)[0] as PaneGroup;
  expect(g.tabs).toEqual(["t1"]);
  expect(g.active).toBe("t1");
});

test("closeTab removing a focused group's last tab hands focus to the group before it", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  const g1 = (groups(l)[0] as PaneGroup).id;
  l = moveTab(l, "t2", { group: g1, zone: "right" }); // left:[t1], right:[t2] (new group last); focus -> right
  const [left, right] = groups(l) as [PaneGroup, PaneGroup];
  expect(left.id).toBe(g1);
  expect(l.focused).toBe(right.id);

  l = closeTab(l, "t2"); // empties and removes the focused (second) group
  expect(groups(l)).toHaveLength(1);
  expect((groups(l)[0] as PaneGroup).id).toBe(g1);
  expect(l.focused).toBe(g1);
});

test("closeTab hands focus to the new first group when the vanishing one led the tree order", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  const g1 = (groups(l)[0] as PaneGroup).id;
  l = moveTab(l, "t2", { group: g1, zone: "left" }); // new group first, g1 second; focus -> new group
  const [first, second] = groups(l) as [PaneGroup, PaneGroup];
  expect(second.id).toBe(g1);
  expect(l.focused).toBe(first.id);

  l = closeTab(l, "t2"); // empties and removes the focused (first) group
  expect(groups(l)).toHaveLength(1);
  expect(l.focused).toBe(g1);
});

test("closeTab on the sole group's last tab leaves it as an empty group, not removed", () => {
  const l = openTab(singlePane(), "t1");
  const closed = closeTab(l, "t1");
  expect(groups(closed)).toHaveLength(1);
  const g = groups(closed)[0] as PaneGroup;
  expect(g.tabs).toEqual([]);
  expect(g.active).toBeNull();
  expect(closed.focused).toBe(g.id);
});

test("closeTab on an unknown tab is a no-op", () => {
  const l = openTab(singlePane(), "t1");
  expect(closeTab(l, "nope")).toBe(l);
});

test("moveTab center within the same group reorders before the given tab", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  l = openTab(l, "t3"); // [t1,t2,t3]
  const gid = (groups(l)[0] as PaneGroup).id;
  l = moveTab(l, "t3", { group: gid, zone: "center", before: "t1" });
  const g = groups(l)[0] as PaneGroup;
  expect(g.tabs).toEqual(["t3", "t1", "t2"]);
  expect(g.active).toBe("t3");
  expect(l.focused).toBe(gid);
});

test("moveTab center with no before moves to the end of its pin class", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  l = openTab(l, "t3"); // [t1,t2,t3]
  const gid = (groups(l)[0] as PaneGroup).id;
  l = pinTab(l, "t1", true); // pinned: [t1]; tabs stay [t1,t2,t3]
  l = moveTab(l, "t2", { group: gid, zone: "center" }); // unpinned, no before: end of the unpinned class
  expect((groups(l)[0] as PaneGroup).tabs).toEqual(["t1", "t3", "t2"]);
});

test("moveTab center across groups empties and removes the source group, landing active+focused in the destination", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  const g1 = (groups(l)[0] as PaneGroup).id;
  l = moveTab(l, "t2", { group: g1, zone: "right" }); // left:[t1] (g1), right:[t2]
  const [, right] = groups(l) as [PaneGroup, PaneGroup];

  l = moveTab(l, "t1", { group: right.id, zone: "center" }); // g1's only tab leaves
  expect(groups(l)).toHaveLength(1); // g1 is gone, not merely empty
  const g = groups(l)[0] as PaneGroup;
  expect(g.id).toBe(right.id);
  expect(g.tabs).toEqual(["t2", "t1"]);
  expect(g.active).toBe("t1");
  expect(l.focused).toBe(right.id);
});

test("moveTab edge creates a row split: new group first for left, last for right", () => {
  const base = openTab(openTab(singlePane(), "t1"), "t2");
  const g1 = (groups(base)[0] as PaneGroup).id;

  const left = moveTab(base, "t2", { group: g1, zone: "left" });
  const leftRoot = left.root as PaneSplit;
  expect(leftRoot.dir).toBe("row");
  expect(leftRoot.sizes).toEqual([0.5, 0.5]);
  const [lfirst, lsecond] = groups(left) as [PaneGroup, PaneGroup];
  expect(lfirst.tabs).toEqual(["t2"]);
  expect(lsecond.id).toBe(g1);

  const right = moveTab(base, "t2", { group: g1, zone: "right" });
  const [rfirst, rsecond] = groups(right) as [PaneGroup, PaneGroup];
  expect(rfirst.id).toBe(g1);
  expect(rsecond.tabs).toEqual(["t2"]);
});

test("moveTab edge creates a column split: new group first for top, last for bottom", () => {
  const base = openTab(openTab(singlePane(), "t1"), "t2");
  const g1 = (groups(base)[0] as PaneGroup).id;

  const top = moveTab(base, "t2", { group: g1, zone: "top" });
  expect((top.root as PaneSplit).dir).toBe("column");
  const [tfirst] = groups(top) as [PaneGroup, PaneGroup];
  expect(tfirst.tabs).toEqual(["t2"]);

  const bottom = moveTab(base, "t2", { group: g1, zone: "bottom" });
  const [, bsecond] = groups(bottom) as [PaneGroup, PaneGroup];
  expect(bsecond.tabs).toEqual(["t2"]);
});

test("moveTab edge is a no-op for a group's only tab dropped onto its own group", () => {
  const l = openTab(singlePane(), "t1");
  const g1 = (groups(l)[0] as PaneGroup).id;
  expect(moveTab(l, "t1", { group: g1, zone: "right" })).toBe(l);
});

test("moveTab edge refuses a split that would exceed MAX_PANES groups", () => {
  let l = singlePane();
  l = openTab(l, "t1");
  l = openTab(l, "t2");
  l = moveTab(l, "t2", { group: (groups(l)[0] as PaneGroup).id, zone: "right" }); // 2 groups
  l = openTab(l, "t3");
  l = moveTab(l, "t3", { group: l.focused, zone: "right" }); // 3 groups
  l = openTab(l, "t4");
  l = moveTab(l, "t4", { group: l.focused, zone: "right" }); // 4 groups == MAX_PANES
  expect(groups(l)).toHaveLength(MAX_PANES);

  l = openTab(l, "t5"); // appended into the focused group, which now holds 2 tabs
  const before = l;
  const refused = moveTab(l, "t5", { group: l.focused, zone: "right" }); // would make a 5th group
  expect(refused).toBe(before);
  expect(groups(refused)).toHaveLength(MAX_PANES);
});

test("normalize flattens nested same-direction splits and keeps proportions", () => {
  const nested: PaneLayout = {
    root: {
      kind: "split",
      id: "s1",
      dir: "row",
      children: [
        {
          kind: "split",
          id: "s2",
          dir: "row",
          children: [
            { kind: "group", id: "g1", tabs: ["t1"], active: "t1" },
            { kind: "group", id: "g2", tabs: ["t2"], active: "t2" },
          ],
          sizes: [0.25, 0.75],
        },
        { kind: "group", id: "g3", tabs: ["t3"], active: "t3" },
      ],
      sizes: [0.4, 0.6],
    },
    focused: "g3",
    pinned: [],
    focusedChat: "g3",
    files: {},
  };
  const flat = normalize(nested);
  const root = flat.root as PaneSplit;
  expect(root.kind).toBe("split");
  expect(root.children.map((c) => c.id)).toEqual(["g1", "g2", "g3"]);
  // g1: 40% of 25% = 10%; g2: 40% of 75% = 30%; g3: 60%. Proportions preserved.
  expect(root.sizes).toEqual([0.1, 0.3, 0.6]);
});

test("retainTabs drops tabs not kept and prunes the emptied group when another survives", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  const g1 = (groups(l)[0] as PaneGroup).id;
  l = moveTab(l, "t2", { group: g1, zone: "right" }); // g1:[t1], other:[t2] (focused)

  const dropped = retainTabs(l, ["t1"]);
  expect(groups(dropped)).toHaveLength(1);
  expect((groups(dropped)[0] as PaneGroup).tabs).toEqual(["t1"]);
});

test("retainTabs keeps one empty group when nothing survives anywhere", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  const g1 = (groups(l)[0] as PaneGroup).id;
  l = moveTab(l, "t2", { group: g1, zone: "right" });

  const emptied = retainTabs(l, []);
  expect(groups(emptied)).toHaveLength(1);
  expect((groups(emptied)[0] as PaneGroup).tabs).toEqual([]);
});

test("pinTab pins and unpins a tab, sorting each group's tabs pinned-first (stable)", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  l = openTab(l, "t3"); // [t1,t2,t3]
  l = pinTab(l, "t3", true);
  expect((groups(l)[0] as PaneGroup).tabs).toEqual(["t3", "t1", "t2"]);
  expect(l.pinned).toEqual(["t3"]);

  l = pinTab(l, "t3", false);
  expect(l.pinned).toEqual([]);
  expect((groups(l)[0] as PaneGroup).tabs).toEqual(["t3", "t1", "t2"]); // order sticks, now unpinned

  l = pinTab(l, "t1", true);
  expect(l.pinned).toEqual(["t1"]);
  expect((groups(l)[0] as PaneGroup).tabs).toEqual(["t1", "t3", "t2"]);
});

test("resize updates a split's sizes; wrong length, non-finite or non-positive numbers are no-ops", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  const g1 = (groups(l)[0] as PaneGroup).id;
  l = moveTab(l, "t2", { group: g1, zone: "right" });
  const splitId = (l.root as PaneSplit).id;

  const resized = resize(l, splitId, [0.3, 0.7]);
  expect((resized.root as PaneSplit).sizes).toEqual([0.3, 0.7]);

  expect(resize(l, splitId, [0.3])).toBe(l); // wrong length
  expect(resize(l, splitId, [0.3, Number.NaN])).toBe(l); // non-finite
  expect(resize(l, splitId, [0.3, 0])).toBe(l); // non-positive
  expect(resize(l, "unknown", [0.5, 0.5])).toBe(l); // unknown split id
});

test("minSize: a group is PANE_MIN; a row sums width and maxes height; a column the reverse", () => {
  const g = (id: string): PaneGroup => ({ kind: "group", id, tabs: [], active: null });
  expect(minSize(g("g1"))).toEqual(PANE_MIN);

  const row: PaneSplit = {
    kind: "split",
    id: "s1",
    dir: "row",
    children: [g("g1"), g("g2")],
    sizes: [0.5, 0.5],
  };
  expect(minSize(row)).toEqual({ width: PANE_MIN.width * 2, height: PANE_MIN.height });

  const column: PaneSplit = { ...row, id: "s2", dir: "column" };
  expect(minSize(column)).toEqual({ width: PANE_MIN.width, height: PANE_MIN.height * 2 });
});

test("normalize is a no-op on an already-normalized layout", () => {
  const l = openTab(singlePane(), "t1");
  expect(normalize(l)).toBe(l);
  const fresh = singlePane();
  expect(normalize(fresh)).toBe(fresh);
});

test("openTab over MAX_TABS evicts the first tab that is neither pinned nor any group's active", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2");
  const a = (groups(l)[0] as PaneGroup).id;
  l = moveTab(l, "t2", { group: a, zone: "right" }); // A:[t1] active t1, B:[t2] active t2, focus -> B
  for (let i = 3; i <= MAX_TABS; i += 1) l = openTab(l, `t${i}`); // B grows to [t2..t64]
  l = pinTab(l, "t3", true);
  expect(groups(l).flatMap((g) => g.tabs)).toHaveLength(MAX_TABS); // exactly at the cap: no eviction yet

  l = openTab(l, "t65"); // one over the cap
  const all = groups(l).flatMap((g) => g.tabs);
  expect(all).toHaveLength(MAX_TABS);
  expect(all).toContain("t1"); // group A's active tab: spared even though it's the oldest
  expect(all).toContain("t3"); // pinned: spared
  expect(all).not.toContain("t2"); // first tab in tree order that is neither pinned nor active
});

test("kindOf: chat by default (empty or a chat tab), file when the first tab is a file id", () => {
  const g = (tabs: string[]): PaneGroup => ({ kind: "group", id: "g1", tabs, active: null });
  expect(kindOf(g([]))).toBe("chat");
  expect(kindOf(g(["t1"]))).toBe("chat");
  expect(kindOf(g(["f1"]))).toBe("file");
  expect(kindOf(g(["f64"]))).toBe("file");
  expect(kindOf(g(["f65"]))).toBe("chat"); // out of range: not a file tab id
});

test("minSize floors a file group at FILE_PANE_MIN and a chat group at PANE_MIN", () => {
  const fileGroup: PaneGroup = { kind: "group", id: "g1", tabs: ["f1"], active: "f1" };
  const chatGroup: PaneGroup = { kind: "group", id: "g2", tabs: ["t1"], active: "t1" };
  expect(minSize(fileGroup)).toEqual(FILE_PANE_MIN);
  expect(minSize(chatGroup)).toEqual(PANE_MIN);
  const row: PaneSplit = {
    kind: "split",
    id: "s1",
    dir: "row",
    children: [fileGroup, chatGroup],
    sizes: [0.5, 0.5],
  };
  expect(minSize(row)).toEqual({
    width: FILE_PANE_MIN.width + PANE_MIN.width,
    height: Math.max(FILE_PANE_MIN.height, PANE_MIN.height),
  });
});

test("openFile with no file group splits focusedChat right when there's room", () => {
  const l = openTab(singlePane(), "t1");
  const opened = openFile(l, FILE_A, () => true) as PaneLayout;
  expect(opened).not.toBeNull();
  expect(groups(opened)).toHaveLength(2);
  const fileGroup = groups(opened).find((g) => kindOf(g) === "file") as PaneGroup;
  expect(fileGroup.tabs).toEqual(["f1"]);
  expect(opened.files.f1).toEqual(FILE_A);
  expect(opened.focused).toBe(fileGroup.id);
  expect(opened.focusedChat).toBe("g1"); // unaffected by the focus move into the file pane
  expect((opened.root as PaneSplit).dir).toBe("row");
});

test("openFile falls back to a bottom split when right has no room", () => {
  const l = openTab(singlePane(), "t1");
  const opened = openFile(l, FILE_A, (_id, edge) => edge === "bottom") as PaneLayout;
  expect((opened.root as PaneSplit).dir).toBe("column");
});

test("openFile returns null when neither edge has room", () => {
  const l = openTab(singlePane(), "t1");
  expect(openFile(l, FILE_A, () => false)).toBeNull();
});

test("openFile reuses the focused file group: adds a new file, reactivates a matching one", () => {
  let l = openTab(singlePane(), "t1");
  l = openFile(l, FILE_A, () => true) as PaneLayout;
  l = openFile(l, FILE_B, () => true) as PaneLayout; // same (focused) file group: adds f2
  let fileGroup = groups(l).find((g) => kindOf(g) === "file") as PaneGroup;
  expect(fileGroup.tabs).toEqual(["f1", "f2"]);
  expect(fileGroup.active).toBe("f2");

  l = openFile(l, FILE_A, () => true) as PaneLayout; // already open: reactivate, don't duplicate
  fileGroup = groups(l).find((g) => kindOf(g) === "file") as PaneGroup;
  expect(fileGroup.tabs).toEqual(["f1", "f2"]);
  expect(fileGroup.active).toBe("f1");
});

test("openFile reuses an existing file group even while a chat group is focused", () => {
  let l = openTab(singlePane(), "t1");
  l = openFile(l, FILE_A, () => true) as PaneLayout;
  const chatId = (groups(l).find((g) => kindOf(g) === "chat") as PaneGroup).id;
  l = focusGroup(l, chatId);
  l = openFile(l, FILE_B, () => true) as PaneLayout;
  expect(groups(l)).toHaveLength(2); // reused the existing file group, not a 3rd
  const fileGroup = groups(l).find((g) => kindOf(g) === "file") as PaneGroup;
  expect(fileGroup.tabs).toEqual(["f1", "f2"]);
});

test("openFile respects MAX_PANES: refuses a split that would exceed it", () => {
  let l = singlePane();
  l = openTab(l, "t1");
  l = openTab(l, "t2");
  l = moveTab(l, "t2", { group: (groups(l)[0] as PaneGroup).id, zone: "right" }); // 2
  l = openTab(l, "t3");
  l = moveTab(l, "t3", { group: l.focused, zone: "right" }); // 3
  l = openTab(l, "t4");
  l = moveTab(l, "t4", { group: l.focused, zone: "right" }); // 4 == MAX_PANES
  expect(groups(l)).toHaveLength(MAX_PANES);
  expect(openFile(l, FILE_A, () => true)).toBeNull(); // no file group to reuse, no room to split
});

test("moveTab center onto an empty group is not redirected; the group adopts the new kind, and the vacated group stays as the chat anchor", () => {
  let l = singlePane();
  const g1 = l.focused;
  l = openFile(l, FILE_A, () => true) as PaneLayout; // g1 stays empty, splits off a file group
  const fileGroupId = (groups(l).find((g) => kindOf(g) === "file") as PaneGroup).id;

  l = moveTab(l, "f1", { group: g1, zone: "center" }); // center onto the still-empty g1
  expect(groups(l)).toHaveLength(2); // the vacated file group survives, empty: the chat anchor
  const adopted = groups(l).find((g) => g.id === g1) as PaneGroup;
  expect(adopted.tabs).toEqual(["f1"]);
  expect(kindOf(adopted)).toBe("file"); // g1 adopted file kind from its first tab
  const anchor = groups(l).find((g) => g.id === fileGroupId) as PaneGroup;
  expect(anchor.tabs).toEqual([]);
  expect(l.focusedChat).toBe(fileGroupId);
});

test("moveTab center onto a different-kind, non-empty group redirects to a right split", () => {
  let l = openTab(singlePane(), "t1");
  l = openTab(l, "t2"); // g1 chat: [t1, t2]
  l = openFile(l, FILE_A, () => true) as PaneLayout; // g1:[t1,t2], file group:[f1] (focused)
  const fileGroupId = (groups(l).find((g) => kindOf(g) === "file") as PaneGroup).id;

  l = moveTab(l, "t2", { group: fileGroupId, zone: "center" }); // mismatched: redirected to right
  expect(groups(l)).toHaveLength(3);
  const fileGroupAfter = groups(l).find((g) => g.id === fileGroupId) as PaneGroup;
  expect(fileGroupAfter.tabs).toEqual(["f1"]); // untouched, not mixed with a chat tab
  const newGroup = groupOf(l, "t2") as PaneGroup;
  expect(newGroup.id).not.toBe(fileGroupId);
  expect(kindOf(newGroup)).toBe("chat");
});

test("splitFile copies the file into a new adjacent group; the original tab is untouched", () => {
  let l = openTab(singlePane(), "t1");
  l = openFile(l, FILE_A, () => true) as PaneLayout;
  const fileGroupId = (groups(l).find((g) => kindOf(g) === "file") as PaneGroup).id;

  l = splitFile(l, "f1", "right");
  expect(groups(l)).toHaveLength(3);
  const original = groups(l).find((g) => g.id === fileGroupId) as PaneGroup;
  expect(original.tabs).toEqual(["f1"]);
  const copy = groups(l).find((g) => g.id !== fileGroupId && kindOf(g) === "file") as PaneGroup;
  expect(copy.tabs).toHaveLength(1);
  const copyTab = copy.tabs[0] as string;
  expect(copyTab).not.toBe("f1");
  expect(l.files[copyTab]).toEqual(FILE_A);
  expect(l.focused).toBe(copy.id);
});

test("splitFile is a no-op for a chat tab or an unknown file tab", () => {
  const l = openTab(singlePane(), "t1");
  expect(splitFile(l, "t1", "right")).toBe(l);
  expect(splitFile(l, "f1", "right")).toBe(l);
});

test("splitFile respects MAX_PANES: refuses a split that would exceed it", () => {
  let l = singlePane();
  l = openTab(l, "t1");
  l = openTab(l, "t2");
  l = moveTab(l, "t2", { group: (groups(l)[0] as PaneGroup).id, zone: "right" }); // 2
  l = openTab(l, "t3");
  l = moveTab(l, "t3", { group: l.focused, zone: "right" }); // 3
  l = openFile(l, FILE_A, () => true) as PaneLayout; // 4 == MAX_PANES
  expect(groups(l)).toHaveLength(MAX_PANES);
  const before = l;
  expect(splitFile(l, "f1", "right")).toBe(before);
});

test("closing a file tab prunes its files entry", () => {
  let l = openTab(singlePane(), "t1");
  l = openFile(l, FILE_A, () => true) as PaneLayout;
  expect(l.files.f1).toEqual(FILE_A);
  l = closeTab(l, "f1");
  expect(l.files.f1).toBeUndefined();
  expect(groups(l)).toHaveLength(1); // the emptied file group was pruned
});

test("focusedChat tracks focus across chat groups, surviving a focus move into a file group", () => {
  let l = openTab(singlePane(), "t1");
  const g1 = l.focused;
  l = openTab(l, "t2");
  l = moveTab(l, "t2", { group: g1, zone: "right" }); // g1:[t1], g2:[t2] (focused)
  expect(l.focusedChat).toBe(l.focused); // g2

  l = focusGroup(l, g1);
  expect(l.focusedChat).toBe(g1);

  l = openFile(l, FILE_A, () => true) as PaneLayout; // splits off focusedChat (g1)
  expect(l.focusedChat).toBe(g1); // kept, even though focus moved to the new file group
  expect(kindOf(focusedGroup(l))).toBe("file");
});

test("closing the only chat tab with one file pane left keeps the chat group, empty", () => {
  let l = openTab(singlePane(), "t1");
  const chatId = l.focused;
  l = openFile(l, FILE_A, () => true) as PaneLayout; // chat:[t1], file:[f1] (file focused)
  expect(l.focusedChat).toBe(chatId);

  l = closeTab(l, "t1");
  expect(groups(l)).toHaveLength(2); // the chat group survives, empty, not pruned
  expect(l.root.kind).toBe("split"); // root stays a split, not collapsed to the lone file group
  const chatGroup = groups(l).find((g) => g.id === chatId) as PaneGroup;
  expect(chatGroup.tabs).toEqual([]);
  expect(chatGroup.active).toBeNull();
  expect(l.focusedChat).toBe(chatId); // still names it
});

test("closing the only chat tab with two file panes left keeps the chat group, empty", () => {
  let l = openTab(singlePane(), "t1");
  const chatId = l.focused;
  l = openFile(l, FILE_A, () => true) as PaneLayout; // chat:[t1], fileA:[f1] (focused)
  l = splitFile(l, "f1", "bottom"); // + fileB: a second view of f1's file
  expect(groups(l)).toHaveLength(3);

  l = closeTab(l, "t1");
  expect(groups(l)).toHaveLength(3); // the chat group survives alongside both file panes
  const chatGroup = groups(l).find((g) => g.id === chatId) as PaneGroup;
  expect(chatGroup.tabs).toEqual([]);
  expect(l.focusedChat).toBe(chatId);
  expect(groups(l).filter((g) => kindOf(g) === "file")).toHaveLength(2);
});

test("retainTabs dropping every chat tab while file panes remain keeps exactly one, empty", () => {
  let l = openTab(singlePane(), "t1");
  const chat1 = l.focused;
  l = openTab(l, "t2");
  l = moveTab(l, "t2", { group: chat1, zone: "right" }); // chat1:[t1], chat2:[t2] (focused)
  const chat2 = l.focused;
  expect(l.focusedChat).toBe(chat2);
  l = openFile(l, FILE_A, () => true) as PaneLayout; // splits a file group off chat2
  expect(groups(l)).toHaveLength(3);

  l = retainTabs(l, ["f1"]); // drops every chat tab; only the file tab survives
  expect(groups(l)).toHaveLength(2); // chat1 is pruned away; chat2 is kept as the one anchor
  expect(groups(l).some((g) => g.id === chat1)).toBe(false);
  const survivor = groups(l).find((g) => g.id === chat2) as PaneGroup;
  expect(survivor.tabs).toEqual([]);
  expect(l.focusedChat).toBe(chat2);
});

test("moving a lone chat tab out by an edge split leaves no extra empty group behind", () => {
  let l = openTab(singlePane(), "t1");
  const chat1 = l.focused;
  l = openFile(l, FILE_A, () => true) as PaneLayout; // chat1:[t1], fileGroup:[f1]
  const fileGroupId = (groups(l).find((g) => kindOf(g) === "file") as PaneGroup).id;

  l = moveTab(l, "t1", { group: fileGroupId, zone: "right" }); // chat1 empties; t1 lands in a fresh group
  expect(groups(l)).toHaveLength(2); // not 3: chat1's emptied node is dropped, not kept alongside the fresh one
  expect(groups(l).some((g) => g.id === chat1)).toBe(false);
  const newChatGroup = groupOf(l, "t1") as PaneGroup;
  expect(newChatGroup.tabs).toEqual(["t1"]);
  expect(l.focusedChat).toBe(newChatGroup.id); // the fresh group, not the vanished chat1
});

test("parseLayout rejects a layout with file groups but no chat group", () => {
  const text = JSON.stringify({
    v: 2,
    focused: "g1",
    pinned: [],
    root: {
      split: "s1",
      dir: "row",
      sizes: [0.5, 0.5],
      children: [
        { group: "g1", tabs: ["f1"], active: "f1" },
        { group: "g2", tabs: ["f2"], active: "f2" },
      ],
    },
    files: { f1: ["w1", "a.md"], f2: ["w1", "b.md"] },
  });
  expect(parseLayout(text)).toBeNull();
});

test("openFile refuses a workspace id or ref that paneFormat's wire format would reject", () => {
  const l = openTab(singlePane(), "t1");
  expect(openFile(l, { workspaceId: "", ref: "a.md" }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w1", ref: "" }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w1", ref: "/abs.md" }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w1", ref: "\\abs.md" }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w1", ref: "a\u0000b" }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w1", ref: "x".repeat(1025) }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w".repeat(65), ref: "a.md" }, () => true)).toBeNull();
  expect(openFile(l, { workspaceId: "w1", ref: "a.md" }, () => true)).not.toBeNull(); // valid: sanity check
});

test("splitFile refuses a file tab whose stored file fails paneFormat's bounds", () => {
  const invalid: PaneLayout = {
    root: { kind: "group", id: "g1", tabs: ["f1"], active: "f1" },
    focused: "g1",
    pinned: [],
    focusedChat: null,
    files: { f1: { workspaceId: "", ref: "a.md" } },
  };
  expect(splitFile(invalid, "f1", "right")).toBe(invalid);
});

test("splitFile evicts over MAX_TABS exactly as openTab does, never leaving more than the cap", () => {
  let l = openTab(singlePane(), "t1");
  for (let i = 2; i <= 63; i += 1) l = openTab(l, `t${i}`); // chat group: 63 tabs, t63 active
  l = openFile(l, FILE_A, () => true) as PaneLayout; // + file group: f1 (64 tabs total, at the cap)
  expect(groups(l).flatMap((g) => g.tabs)).toHaveLength(MAX_TABS);

  l = splitFile(l, "f1", "right"); // copies f1 into a second file tab: one over the cap
  const all = groups(l).flatMap((g) => g.tabs);
  expect(all).toHaveLength(MAX_TABS); // evicted back down to the cap, never left over it
  expect(all).not.toContain("t1"); // first tab in tree order that is neither pinned nor active
  expect(groups(l).filter((g) => kindOf(g) === "file")).toHaveLength(2);
});

function chatId(n: number): string {
  return `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;
}

function pinnedChats(count: number): PaneLayout {
  let l = singlePane();
  for (let n = 1; n <= count; n += 1) l = pinTab(openTab(l, chatId(n)), chatId(n), true);
  expect(parseLayout(serializeLayout(l))).not.toBeNull();
  return l;
}

function fullFileLayout(pinned: boolean): PaneLayout {
  let l = singlePane();
  for (let n = 1; n <= MAX_TABS; n += 1) {
    const next = openFile(l, { workspaceId: "w1", ref: `src/file${n}.ts` }, () => true);
    expect(next).not.toBeNull();
    l = next as PaneLayout;
    if (pinned) l = pinTab(l, `f${n}`, true);
  }
  expect(parseLayout(serializeLayout(l))).not.toBeNull();
  return l;
}

test("openFile returns null at the pinned chat cap without changing the reloadable layout", () => {
  const l = pinnedChats(MAX_TABS);
  const before = serializeLayout(l);
  expect(openFile(l, FILE_A, () => true)).toBeNull();
  expect(serializeLayout(l)).toBe(before);
});

test("openFile refuses a full pinned file budget without throwing and still selects an existing file", () => {
  const l = fullFileLayout(true);
  expect(groups(l).find((g) => kindOf(g) === "chat")?.tabs).toEqual([]);
  expect(openFile(l, FILE_A, () => true)).toBeNull();
  const selected = openFile(l, l.files.f1 as typeof FILE_A, () => false) as PaneLayout;
  expect(selected).not.toBeNull();
  expect(focusedGroup(selected).active).toBe("f1");
  expect(selected.files).toEqual(l.files);
  expect(selected.pinned).toEqual(l.pinned);
  expect(groups(selected).flatMap((g) => g.tabs)).toHaveLength(MAX_TABS);
  expect(parseLayout(serializeLayout(selected))).toEqual(selected);
});

test.each(["chat", "file"] as const)(
  "splitFile refuses at a protected %s tab cap and keeps the input layout",
  (kind) => {
    const l =
      kind === "file"
        ? fullFileLayout(true)
        : pinTab(openFile(pinnedChats(MAX_TABS - 1), FILE_A, () => true) as PaneLayout, "f1", true);
    expect(parseLayout(serializeLayout(l))).not.toBeNull();
    expect(splitFile(l, "f1", "right")).toBe(l);
    expect(groups(l).flatMap((g) => g.tabs)).toHaveLength(MAX_TABS);
  },
);

test("openFile reuses an eligible file id when every file id is allocated", () => {
  const l = fullFileLayout(false);
  const next = openFile(l, FILE_A, () => true) as PaneLayout;
  expect(next).not.toBeNull();
  expect(next.files.f1).toEqual(FILE_A);
  expect(next.files.f64).toEqual(l.files.f64);
  expect(focusedGroup(next).active).toBe("f1");
  expect(Object.values(next.files).some((file) => file.ref === "src/file1.ts")).toBe(false);
  expect(groups(next).flatMap((g) => g.tabs)).toHaveLength(MAX_TABS);
  expect(Object.keys(next.files)).toHaveLength(MAX_TABS);
  expect(parseLayout(serializeLayout(next))).toEqual(next);
  expect(l.files.f1?.ref).toBe("src/file1.ts");
});

test("splitFile reuses an eligible file id and keeps the original active file", () => {
  const l = fullFileLayout(false);
  const next = splitFile(l, "f64", "right");
  expect(groups(next)).toHaveLength(3);
  expect(groupOf(next, "f64")?.active).toBe("f64");
  expect(focusedGroup(next).active).toBe("f1");
  expect(next.files.f1).toEqual(l.files.f64);
  expect(groups(next).flatMap((g) => g.tabs)).toHaveLength(MAX_TABS);
  expect(Object.keys(next.files)).toHaveLength(MAX_TABS);
  expect(parseLayout(serializeLayout(next))).toEqual(next);
});

test("appending a file can evict the destination's previous active tab when it is the only candidate", () => {
  const l = openFile(pinnedChats(MAX_TABS - 1), FILE_A, () => true) as PaneLayout;
  const next = openFile(l, FILE_B, () => true) as PaneLayout;
  expect(next).not.toBeNull();
  expect(focusedGroup(next).id).toBe(focusedGroup(l).id);
  expect(Object.values(next.files)).toEqual([FILE_B]);
  expect(next.files[focusedGroup(next).active as string]).toEqual(FILE_B);
  expect(next.pinned).toEqual(l.pinned);
  expect(groups(next).flatMap((g) => g.tabs)).toHaveLength(MAX_TABS);
  expect(parseLayout(serializeLayout(next))).toEqual(next);
});

test("opening the first file at the chat cap evicts the first eligible inactive chat", () => {
  const l = pinTab(pinnedChats(MAX_TABS), chatId(1), false);
  const next = openFile(l, FILE_A, () => true) as PaneLayout;
  expect(next).not.toBeNull();
  expect(groupOf(next, chatId(1))).toBeUndefined();
  expect(groupOf(next, chatId(MAX_TABS))?.active).toBe(chatId(MAX_TABS));
  expect(next.files.f1).toEqual(FILE_A);
  expect(groups(next).flatMap((g) => g.tabs)).toHaveLength(MAX_TABS);
  expect(parseLayout(serializeLayout(next))).toEqual(next);
});
