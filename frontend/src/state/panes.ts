import { isValidRef, isValidWorkspaceId } from "./paneFormat";
import { moveTabTo, orderTabs } from "./sessionOrganization";

/**
 * A pane layout is a binary tree of splits down to groups of tabs. Every exported operation
 * takes and returns a normalized `PaneLayout`: a split has >= 2 children and never directly
 * holds a same-direction child split (those flatten into it); a split's `sizes` sum to 1 (4
 * decimals); an empty group is dropped unless the whole tree would otherwise hold none, or it's
 * the last chat group left while a file group survives; a tab lives in exactly one
 * group; `pinned` is a subset of the open tabs; and each group's own `tabs` list keeps pinned
 * tabs first (stable order otherwise). An operation that would not change any of this returns
 * its input `l` as-is, so callers can compare by reference.
 */

export const MAX_PANES = 4;
export const MAX_TABS = 64;
export const PANE_MIN = { width: 420, height: 240 };
export const FILE_PANE_MIN = { width: 320, height: 160 };

export type Edge = "left" | "right" | "top" | "bottom";
export type Zone = Edge | "center";

export interface PaneGroup {
  kind: "group";
  id: string;
  tabs: readonly string[];
  active: string | null;
}

export interface PaneSplit {
  kind: "split";
  id: string;
  dir: "row" | "column";
  children: readonly PaneNode[];
  sizes: readonly number[];
}

export type PaneNode = PaneGroup | PaneSplit;

export interface PaneLayout {
  root: PaneNode;
  focused: string;
  pinned: readonly string[];
  /** The last focused group that held chats; survives a focus move into a file group so a
   * save (see `state/usePanes.ts`'s `structureOf`) never loses track of it. Derived, never
   * hand-set: kept current by every focus, open, move and close. */
  focusedChat: string | null;
  /** File tabs' identity, keyed by their `f1..f64` tab id. A chat tab has no entry here. */
  files: Readonly<Record<string, { workspaceId: string; ref: string }>>;
}

// -- tree primitives ---------------------------------------------------------------------

function group(id: string, tabs: readonly string[], active: string | null): PaneGroup {
  return { kind: "group", id, tabs, active };
}

function split(
  id: string,
  dir: PaneSplit["dir"],
  children: readonly PaneNode[],
  sizes: readonly number[],
): PaneSplit {
  return { kind: "split", id, dir, children, sizes };
}

function groupsOf(node: PaneNode): PaneGroup[] {
  return node.kind === "group" ? [node] : node.children.flatMap(groupsOf);
}

/** Replace the group with `id` via `fn`; every other node is left as a plain tree rebuild. */
function mapGroup(node: PaneNode, id: string, fn: (g: PaneGroup) => PaneGroup): PaneNode {
  if (node.kind === "group") return node.id === id ? fn(node) : node;
  return { ...node, children: node.children.map((child) => mapGroup(child, id, fn)) };
}

/** Replace the node with `id` by `next`, wherever it sits in the tree. */
function replaceNode(node: PaneNode, id: string, next: PaneNode): PaneNode {
  if (node.id === id) return next;
  if (node.kind === "group") return node;
  return { ...node, children: node.children.map((child) => replaceNode(child, id, next)) };
}

/** Drop `tab` from whichever group holds it. If it was that group's active tab, active passes
 * to the tab now at its old index, else the one before, else null. */
function dropTab(node: PaneNode, tab: string): PaneNode {
  if (node.kind === "group") {
    const at = node.tabs.indexOf(tab);
    if (at < 0) return node;
    const tabs = node.tabs.filter((t) => t !== tab);
    const active = node.active !== tab ? node.active : (tabs[at] ?? tabs[at - 1] ?? null);
    return group(node.id, tabs, active);
  }
  return { ...node, children: node.children.map((child) => dropTab(child, tab)) };
}

function collectIds(node: PaneNode, out: Set<string> = new Set()): Set<string> {
  out.add(node.id);
  if (node.kind === "split") for (const child of node.children) collectIds(child, out);
  return out;
}

/** The smallest unused `g1..g9` (groups) or `s1..s9` (splits); MAX_PANES keeps this well
 * under 9 in practice (at most 4 groups and 3 splits). */
function freeId(ids: ReadonlySet<string>, prefix: "g" | "s"): string {
  for (let n = 1; n <= 9; n += 1) {
    const id = `${prefix}${n}`;
    if (!ids.has(id)) return id;
  }
  throw new Error(`panes: no free ${prefix} id`);
}

const FILE_TAB = /^f([1-9]|[1-5][0-9]|6[0-4])$/;

function isFileTab(tab: string): boolean {
  return FILE_TAB.test(tab);
}

/** The smallest unused `f1..f64` file tab id (MAX_TABS keeps this in range: files share the
 * same 64-tab budget as chats). */
function freeFileId(files: Readonly<Record<string, unknown>>): string {
  for (let n = 1; n <= MAX_TABS; n += 1) {
    const id = `f${n}`;
    if (!(id in files)) return id;
  }
  throw new Error("panes: no free file id");
}

/** "file" when the group's first tab is a file tab id, else "chat" (including when the group
 * is empty — an empty group defaults to chat until it gets its first tab). Groups are
 * homogeneous, so the first tab speaks for the whole group. */
export function kindOf(g: PaneGroup): "chat" | "file" {
  const first = g.tabs[0];
  return first !== undefined && isFileTab(first) ? "file" : "chat";
}

// -- normalization ------------------------------------------------------------------------

function roundTo4(n: number): number {
  return Math.round(n * 10000) / 10000;
}

/** Scale `sizes` to sum to exactly 1, rounded to 4 decimals (rounding drift lands on the last
 * entry, so the total stays exact). */
function shareSizes(sizes: readonly number[]): readonly number[] {
  const total = sizes.reduce((sum, s) => sum + s, 0) || 1;
  const scaled = sizes.map((s) => roundTo4(s / total));
  const last = scaled.length - 1;
  const drift = 1 - scaled.reduce((sum, s) => sum + s, 0);
  return scaled.map((s, i) => (i === last ? roundTo4(s + drift) : s));
}

/** Bottom-up: drop empty groups except `keepEmpty`'s (the chat anchor — see `canonicalize`),
 * flatten a split's same-direction child splits into itself (scaling their share of the slot),
 * and collapse a split left with one child into that child. Returns null when everything under
 * `node` was empty; `canonicalize` is the one that falls back to a single fresh group so a
 * layout never ends with zero groups. */
function shape(node: PaneNode, keepEmpty?: string): PaneNode | null {
  if (node.kind === "group") return node.tabs.length === 0 && node.id !== keepEmpty ? null : node;
  const entries = node.children
    .map((child, i) => ({ shaped: shape(child, keepEmpty), size: node.sizes[i] ?? 0 }))
    .filter((e): e is { shaped: PaneNode; size: number } => e.shaped !== null)
    .flatMap((e) => {
      const child = e.shaped;
      if (child.kind === "split" && child.dir === node.dir) {
        const total = child.sizes.reduce((sum, s) => sum + s, 0) || 1;
        return child.children.map((grandchild, j) => ({
          shaped: grandchild,
          size: (e.size * (child.sizes[j] ?? 0)) / total,
        }));
      }
      return [e];
    });
  if (entries.length === 0) return null;
  if (entries.length === 1) {
    const [only] = entries;
    return only ? only.shaped : null;
  }
  return split(
    node.id,
    node.dir,
    entries.map((e) => e.shaped),
    shareSizes(entries.map((e) => e.size)),
  );
}

function sortGroupTabs(node: PaneNode, pinned: readonly string[]): PaneNode {
  if (node.kind === "group") return group(node.id, orderTabs(node.tabs, pinned), node.active);
  return { ...node, children: node.children.map((child) => sortGroupTabs(child, pinned)) };
}

/** `focused` itself when it's a chat group; else `prevFocusedChat` when that group still
 * exists and is still chat; else the first chat group in tree order; else null. The same rule
 * serves a live op (real `prevFocusedChat`) and a fresh parse (`prevFocusedChat` always null,
 * which skips straight to the tree-order fallback). */
function deriveFocusedChat(
  liveGroups: readonly PaneGroup[],
  focused: string,
  prevFocusedChat: string | null,
): string | null {
  const focusedGroupNode = liveGroups.find((g) => g.id === focused);
  if (focusedGroupNode && kindOf(focusedGroupNode) === "chat") return focused;
  if (
    prevFocusedChat !== null &&
    liveGroups.some((g) => g.id === prevFocusedChat && kindOf(g) === "chat")
  ) {
    return prevFocusedChat;
  }
  const firstChat = liveGroups.find((g) => kindOf(g) === "chat");
  return firstChat ? firstChat.id : null;
}

function pruneFiles(
  files: PaneLayout["files"],
  liveTabs: ReadonlySet<string>,
): PaneLayout["files"] {
  return Object.fromEntries(Object.entries(files).filter(([id]) => liveTabs.has(id)));
}

/** The empty group to exempt from `shape`'s drop so the layout keeps one chat group
 * while any file group survives — `preferred` (`focusedChat` going in) when it's in the tree
 * and empty, else the first empty group in tree order. Null when none is empty: a public
 * mutator always leaves a vanishing chat group's node behind (empty, not yet removed) for this
 * to find, so null here means there was no chat group to begin with. */
function pickEmptyAnchor(root: PaneNode, preferred: string | null): string | null {
  const empty = groupsOf(root).filter((g) => g.tabs.length === 0);
  if (preferred !== null && empty.some((g) => g.id === preferred)) return preferred;
  return empty[0]?.id ?? null;
}

function canonicalize(candidate: PaneLayout): PaneLayout {
  const plain = shape(candidate.root);
  const plainGroups = plain ? groupsOf(plain) : [];
  const losesLastChat =
    plainGroups.some((g) => kindOf(g) === "file") && !plainGroups.some((g) => kindOf(g) === "chat");
  const anchor = losesLastChat ? pickEmptyAnchor(candidate.root, candidate.focusedChat) : null;
  const shaped =
    (anchor ? shape(candidate.root, anchor) : plain) ?? group(candidate.focused, [], null);
  const liveTabs = new Set(groupsOf(shaped).flatMap((g) => g.tabs));
  const pinned = candidate.pinned.filter((id) => liveTabs.has(id));
  const root = sortGroupTabs(shaped, pinned);
  const liveGroups = groupsOf(root);
  const focused = liveGroups.some((g) => g.id === candidate.focused)
    ? candidate.focused
    : (liveGroups[0] as PaneGroup).id;
  const files = pruneFiles(candidate.files, liveTabs);
  const focusedChat = deriveFocusedChat(liveGroups, focused, candidate.focusedChat);
  return { root, focused, pinned, files, focusedChat };
}

function sameNode(a: PaneNode, b: PaneNode): boolean {
  if (a.kind === "group" && b.kind === "group")
    return (
      a.id === b.id &&
      a.active === b.active &&
      a.tabs.length === b.tabs.length &&
      a.tabs.every((t, i) => t === b.tabs[i])
    );
  if (a.kind === "split" && b.kind === "split")
    return (
      a.id === b.id &&
      a.dir === b.dir &&
      a.children.length === b.children.length &&
      a.sizes.every((s, i) => s === b.sizes[i]) &&
      a.children.every((c, i) => sameNode(c, b.children[i] as PaneNode))
    );
  return false;
}

function sameFiles(a: PaneLayout["files"], b: PaneLayout["files"]): boolean {
  const aIds = Object.keys(a);
  if (aIds.length !== Object.keys(b).length) return false;
  return aIds.every((id) => {
    const bf = b[id];
    return bf !== undefined && a[id]?.workspaceId === bf.workspaceId && a[id]?.ref === bf.ref;
  });
}

function sameLayout(a: PaneLayout, b: PaneLayout): boolean {
  return (
    a.focused === b.focused &&
    a.focusedChat === b.focusedChat &&
    a.pinned.length === b.pinned.length &&
    a.pinned.every((p, i) => p === b.pinned[i]) &&
    sameFiles(a.files, b.files) &&
    sameNode(a.root, b.root)
  );
}

/** Canonicalize `candidate`; if that equals `original`, return `original` so callers can
 * compare an op's result by reference. Every exported mutator funnels through this. */
function finish(original: PaneLayout, candidate: PaneLayout): PaneLayout {
  const next = canonicalize(candidate);
  return sameLayout(next, original) ? original : next;
}

// -- public API -----------------------------------------------------------------------------

export function singlePane(): PaneLayout {
  return { root: group("g1", [], null), focused: "g1", pinned: [], focusedChat: "g1", files: {} };
}

export function groups(l: PaneLayout): readonly PaneGroup[] {
  return groupsOf(l.root);
}

export function groupOf(l: PaneLayout, tab: string): PaneGroup | undefined {
  return groups(l).find((g) => g.tabs.includes(tab));
}

export function focusedGroup(l: PaneLayout): PaneGroup {
  const list = groups(l);
  return (list.find((g) => g.id === l.focused) ?? list[0]) as PaneGroup;
}

export function normalize(l: PaneLayout): PaneLayout {
  return finish(l, l);
}

/** Over `limit`, drop the first tab in tree order that is neither pinned nor any group's
 * active tab. */
function evictOverCap(l: PaneLayout, limit = MAX_TABS): PaneLayout {
  const all = groups(l).flatMap((g) => g.tabs);
  if (all.length <= limit) return l;
  const pinned = new Set(l.pinned);
  const active = new Set(groups(l).flatMap((g) => (g.active !== null ? [g.active] : [])));
  const evict = all.find((t) => !pinned.has(t) && !active.has(t));
  return evict === undefined ? l : { ...l, root: dropTab(l.root, evict) };
}

export function openTab(l: PaneLayout, tab: string): PaneLayout {
  const existing = groupOf(l, tab);
  const targetId = existing ? existing.id : focusedGroup(l).id;
  const root = mapGroup(l.root, targetId, (g) =>
    existing ? group(g.id, g.tabs, tab) : group(g.id, [...g.tabs, tab], tab),
  );
  return finish(l, evictOverCap({ ...l, root, focused: targetId }));
}

export function focusGroup(l: PaneLayout, id: string): PaneLayout {
  const target = groups(l).find((g) => g.id === id);
  if (!target) return l;
  const root =
    target.active === null && target.tabs.length > 0
      ? mapGroup(l.root, id, (g) => group(g.id, g.tabs, g.tabs[g.tabs.length - 1] ?? null))
      : l.root;
  return finish(l, { ...l, root, focused: id });
}

export function clearActive(l: PaneLayout): PaneLayout {
  const target = focusedGroup(l);
  return finish(l, { ...l, root: mapGroup(l.root, target.id, (g) => group(g.id, g.tabs, null)) });
}

export function closeTab(l: PaneLayout, tab: string): PaneLayout {
  const order = groups(l);
  const owner = order.find((g) => g.tabs.includes(tab));
  if (!owner) return l;
  let focused = l.focused;
  if (owner.id === l.focused && owner.tabs.length === 1 && order.length > 1) {
    const at = order.findIndex((g) => g.id === owner.id);
    const before = at > 0 ? order[at - 1] : order[1];
    focused = (before as PaneGroup).id;
  }
  return finish(l, { ...l, root: dropTab(l.root, tab), focused });
}

export function moveTab(
  l: PaneLayout,
  tab: string,
  target: { group: string; zone: Zone; before?: string },
): PaneLayout {
  const destGroup = groups(l).find((g) => g.id === target.group);
  if (!destGroup) return l;
  const source = groupOf(l, tab);

  // Groups are homogeneous (chat tabs and file tabs never share one): a center drop that would
  // mix kinds is redirected to a right-edge split instead.
  const mismatched =
    target.zone === "center" &&
    destGroup.tabs.length > 0 &&
    kindOf(destGroup) !== (isFileTab(tab) ? "file" : "chat");
  const zone: Zone = mismatched ? "right" : target.zone;

  if (zone === "center") {
    if (source && source.id === destGroup.id) {
      const tabs = moveTabTo(destGroup.tabs, tab, target.before ?? null, l.pinned);
      const root = mapGroup(l.root, destGroup.id, (g) => group(g.id, tabs, tab));
      return finish(l, { ...l, root, focused: destGroup.id });
    }
    const withoutTab = source ? dropTab(l.root, tab) : l.root;
    const root = mapGroup(withoutTab, destGroup.id, (g) => {
      const at = target.before ? g.tabs.indexOf(target.before) : -1;
      const tabs = at < 0 ? [...g.tabs, tab] : [...g.tabs.slice(0, at), tab, ...g.tabs.slice(at)];
      return group(g.id, tabs, tab);
    });
    return finish(l, { ...l, root, focused: destGroup.id });
  }

  // Edge: wrap the target group in a new split holding a fresh group with `tab`.
  if (source && source.id === destGroup.id && source.tabs.length === 1) return l;
  const withoutTab = source ? dropTab(l.root, tab) : l.root;
  const dest = groupsOf(withoutTab).find((g) => g.id === destGroup.id);
  if (!dest) return l;
  const ids = collectIds(l.root);
  const freshId = freeId(ids, "g");
  const fresh = group(freshId, [tab], tab);
  const dir: PaneSplit["dir"] = zone === "left" || zone === "right" ? "row" : "column";
  const firstIsFresh = zone === "left" || zone === "top";
  const wrapped = split(
    freeId(ids, "s"),
    dir,
    firstIsFresh ? [fresh, dest] : [dest, fresh],
    [0.5, 0.5],
  );
  const root = replaceNode(withoutTab, destGroup.id, wrapped);
  const result = finish(l, { ...l, root, focused: freshId });
  return groups(result).length > MAX_PANES ? l : result;
}

function sameFile(
  a: { workspaceId: string; ref: string } | undefined,
  b: { workspaceId: string; ref: string },
): boolean {
  return a !== undefined && a.workspaceId === b.workspaceId && a.ref === b.ref;
}

/** Reserve capacity before allocating a file id. Appending replaces `targetId`'s active
 * selection, so its former active tab can be evicted; a split keeps existing selections. */
function reserveFileTab(l: PaneLayout, targetId?: string): PaneLayout | null {
  const root = targetId ? mapGroup(l.root, targetId, (g) => group(g.id, g.tabs, null)) : l.root;
  const next = evictOverCap({ ...l, root }, MAX_TABS - 1);
  const liveTabs = new Set(groups(next).flatMap((g) => g.tabs));
  if (liveTabs.size >= MAX_TABS) return null;
  return { ...next, files: pruneFiles(next.files, liveTabs) };
}

/** Wraps `anchorId`'s group in a new split (new group first for left/top, last for
 * right/bottom, sized evenly) holding a fresh group with one new file tab copied from `file`,
 * reserving capacity within MAX_TABS. Null when `anchorId` doesn't resolve or no tab can make
 * room. */
function splitOffFile(
  l: PaneLayout,
  anchorId: string,
  edge: Edge,
  file: { workspaceId: string; ref: string },
): PaneLayout | null {
  const next = reserveFileTab(l);
  if (!next) return null;
  const anchor = groupsOf(next.root).find((g) => g.id === anchorId);
  if (!anchor) return null;
  const ids = collectIds(next.root);
  const fileId = freeFileId(next.files);
  const freshId = freeId(ids, "g");
  const fresh = group(freshId, [fileId], fileId);
  const dir: PaneSplit["dir"] = edge === "left" || edge === "right" ? "row" : "column";
  const firstIsFresh = edge === "left" || edge === "top";
  const wrapped = split(
    freeId(ids, "s"),
    dir,
    firstIsFresh ? [fresh, anchor] : [anchor, fresh],
    [0.5, 0.5],
  );
  const root = replaceNode(next.root, anchorId, wrapped);
  return finish(l, { ...next, root, focused: freshId, files: { ...next.files, [fileId]: file } });
}

/** Opens `file`: reuses the most recently focused file group (activating a matching tab there,
 * or adding one), else splits a fresh file group off `focusedChat` — right, then bottom,
 * whichever `room` allows — else null when neither fits or both would exceed MAX_PANES. Null
 * too when no tab can make room within MAX_TABS or `file` fails paneFormat's own bounds. */
export function openFile(
  l: PaneLayout,
  file: { workspaceId: string; ref: string },
  room: (groupId: string, edge: Edge) => boolean,
): PaneLayout | null {
  if (!isValidWorkspaceId(file.workspaceId) || !isValidRef(file.ref)) return null;
  const current = focusedGroup(l);
  const target = kindOf(current) === "file" ? current : groups(l).find((g) => kindOf(g) === "file");

  if (target) {
    const existing = target.tabs.find((t) => sameFile(l.files[t], file));
    if (existing !== undefined) {
      const root = mapGroup(l.root, target.id, (g) => group(g.id, g.tabs, existing));
      return finish(l, { ...l, root, focused: target.id });
    }
    const next = reserveFileTab(l, target.id);
    if (!next) return null;
    const id = freeFileId(next.files);
    const root = mapGroup(next.root, target.id, (g) => group(g.id, [...g.tabs, id], id));
    return finish(l, { ...next, root, focused: target.id, files: { ...next.files, [id]: file } });
  }

  const chatId = l.focusedChat;
  if (chatId === null) return null;
  for (const edge of ["right", "bottom"] as const) {
    if (!room(chatId, edge)) continue;
    const result = splitOffFile(l, chatId, edge, file);
    if (result && groups(result).length <= MAX_PANES) return result;
  }
  return null;
}

/** Splits `tab` (an existing file tab) into a new adjacent group holding a copy of the same
 * file — an intentional second view, unlike `openFile`'s dedup. No-op if `tab` isn't a known file
 * tab, its file fails paneFormat's bounds, or no tab can make room within MAX_TABS or the
 * split would exceed MAX_PANES. */
export function splitFile(l: PaneLayout, tab: string, edge: Edge): PaneLayout {
  const file = l.files[tab];
  const anchorId = groupOf(l, tab)?.id;
  if (!file || !anchorId || !isValidWorkspaceId(file.workspaceId) || !isValidRef(file.ref))
    return l;
  const result = splitOffFile(l, anchorId, edge, file);
  if (!result) return l;
  return groups(result).length > MAX_PANES ? l : result;
}

export function pinTab(l: PaneLayout, tab: string, pinned: boolean): PaneLayout {
  const next = pinned ? [...new Set([...l.pinned, tab])] : l.pinned.filter((id) => id !== tab);
  return finish(l, { ...l, pinned: next });
}

function findSplit(node: PaneNode, id: string): PaneSplit | undefined {
  if (node.kind !== "split") return undefined;
  if (node.id === id) return node;
  for (const child of node.children) {
    const found = findSplit(child, id);
    if (found) return found;
  }
  return undefined;
}

export function resize(l: PaneLayout, splitId: string, sizes: readonly number[]): PaneLayout {
  const target = findSplit(l.root, splitId);
  if (!target) return l;
  if (sizes.length !== target.children.length) return l;
  if (sizes.some((s) => !Number.isFinite(s) || s <= 0)) return l;
  const root = replaceNode(l.root, splitId, split(target.id, target.dir, target.children, sizes));
  return finish(l, { ...l, root });
}

export function retainTabs(l: PaneLayout, keep: Iterable<string>): PaneLayout {
  const keepSet = new Set(keep);
  const prune = (node: PaneNode): PaneNode => {
    if (node.kind === "group") {
      const tabs = node.tabs.filter((t) => keepSet.has(t));
      const active =
        node.active !== null && tabs.includes(node.active)
          ? node.active
          : (tabs[tabs.length - 1] ?? null);
      return group(node.id, tabs, active);
    }
    return { ...node, children: node.children.map(prune) };
  };
  return finish(l, { ...l, root: prune(l.root) });
}

export function minSize(node: PaneNode): { width: number; height: number } {
  if (node.kind === "group") return kindOf(node) === "file" ? FILE_PANE_MIN : PANE_MIN;
  const sizes = node.children.map(minSize);
  return node.dir === "row"
    ? {
        width: sizes.reduce((sum, s) => sum + s.width, 0),
        height: Math.max(...sizes.map((s) => s.height)),
      }
    : {
        width: Math.max(...sizes.map((s) => s.width)),
        height: sizes.reduce((sum, s) => sum + s.height, 0),
      };
}
