import type { PaneLayout, PaneNode, PaneSplit } from "./panes";
import { groups, MAX_PANES, MAX_TABS, normalize } from "./panes";

/**
 * The wire form of a `PaneLayout`: a versioned JSON string, capped in size so a corrupt or
 * hand-edited setting can never balloon. Wire nodes spell their id as the `group`/`split` key
 * itself, so the key set alone proves which kind of node it is: `{ group, tabs, active }` or
 * `{ split, dir, sizes, children }`. `parseLayout` is fail-closed: any size, shape, count, or
 * cross-reference problem returns `null` instead of throwing, so a caller always has a clean
 * fallback. A successful parse is handed to `normalize` so it comes back canonical.
 *
 * v2 adds `files`, wire-compact as `{ "fN": [workspaceId, ref] }`; v1 (no `files` key) still
 * parses. `serializeLayout` only reaches for v2 when there's at least one file tab, so a
 * files-less layout keeps producing byte-identical v1 text. `focusedChat` is window-local
 * and never written; a parse derives it (see `panes.ts`'s `deriveFocusedChat`).
 */

export const MAX_LAYOUT_CHARS = 8192;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const GROUP_ID = /^g[1-9]$/;
const SPLIT_ID = /^s[1-9]$/;
const FILE_ID = /^f([1-9]|[1-5][0-9]|6[0-4])$/;
const MAX_NODES = 7;
const WORKSPACE_ID_MAX_CHARS = 64;
const REF_MAX_CHARS = 1024;

/** True if `value` holds a C0 control character or DEL. Checked by code point (not a regex
 * literal) because a literal control character in a regex is itself flagged as suspicious. */
function hasControlChar(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

type WireNode =
  | { group: string; tabs: readonly string[]; active: string | null }
  | { split: string; dir: PaneSplit["dir"]; sizes: readonly number[]; children: WireNode[] };

type WireFiles = Record<string, readonly [string, string]>;

/** `dropped` tab ids are left out of every group's wire `tabs`, so the tree and `files` stay
 * cross-reference-consistent while `serializeLayout` is shrinking toward MAX_LAYOUT_CHARS. */
function toWire(node: PaneNode, dropped?: ReadonlySet<string>): WireNode {
  if (node.kind === "group") {
    const tabs = dropped && dropped.size > 0 ? node.tabs.filter((t) => !dropped.has(t)) : node.tabs;
    return { group: node.id, tabs, active: node.active };
  }
  return {
    split: node.id,
    dir: node.dir,
    sizes: node.sizes,
    children: node.children.map((child) => toWire(child, dropped)),
  };
}

/** `files`, minus `dropped`, as `{ "fN": [workspaceId, ref] }` ordered by ascending file
 * number — independent of `files`' own (insertion) key order, so the same set of files always
 * serializes to the same text. */
function toWireFiles(files: PaneLayout["files"], dropped: ReadonlySet<string>): WireFiles {
  const ids = Object.keys(files)
    .filter((id) => !dropped.has(id))
    .sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
  const out: WireFiles = {};
  for (const id of ids) {
    const f = files[id];
    if (f) out[id] = [f.workspaceId, f.ref];
  }
  return out;
}

/** Inactive file tabs eligible for eviction, last group first (then each group's own tab
 * order): the drop order `serializeLayout` walks when the text is still too long. */
function droppableFileTabs(l: PaneLayout): string[] {
  const out: string[] = [];
  for (const g of [...groups(l)].reverse()) {
    for (const tab of g.tabs) {
      if (tab !== g.active && tab in l.files) out.push(tab);
    }
  }
  return out;
}

export function serializeLayout(l: PaneLayout): string {
  if (Object.keys(l.files).length === 0) {
    return JSON.stringify({ v: 1, focused: l.focused, pinned: l.pinned, root: toWire(l.root) });
  }
  const candidates = droppableFileTabs(l);
  const dropped = new Set<string>();
  for (;;) {
    const text = JSON.stringify({
      v: 2,
      focused: l.focused,
      pinned: l.pinned,
      root: toWire(l.root, dropped),
      files: toWireFiles(l.files, dropped),
    });
    const next = candidates.find((tab) => !dropped.has(tab));
    if (text.length <= MAX_LAYOUT_CHARS || !next) return text;
    dropped.add(next);
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactKeys(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  if (!isPlainObject(value) || Object.keys(value).length !== keys.length) return false;
  return keys.every((key) => key in value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isSizes(value: unknown, length: number): value is number[] {
  return (
    Array.isArray(value) &&
    value.length === length &&
    value.every((s) => typeof s === "number" && Number.isFinite(s) && s > 0)
  );
}

export function isValidWorkspaceId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= WORKSPACE_ID_MAX_CHARS &&
    !hasControlChar(value)
  );
}

export function isValidRef(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= REF_MAX_CHARS &&
    !hasControlChar(value) &&
    !value.startsWith("/") &&
    !value.startsWith("\\")
  );
}

/** Validates v2's wire `files`: every key a file tab id, every value a `[workspaceId, ref]`
 * pair within bounds. Null on any problem, same fail-closed contract as the rest of this
 * parser; cross-referencing those ids against the tree is `parseLayout`'s job below. */
function parseFiles(value: unknown): PaneLayout["files"] | null {
  if (!isPlainObject(value)) return null;
  const out: Record<string, { workspaceId: string; ref: string }> = {};
  for (const [id, entry] of Object.entries(value)) {
    if (!FILE_ID.test(id)) return null;
    if (!Array.isArray(entry) || entry.length !== 2) return null;
    const [workspaceId, ref] = entry;
    if (!isValidWorkspaceId(workspaceId) || !isValidRef(ref)) return null;
    out[id] = { workspaceId, ref };
  }
  return out;
}

/** Parses and validates a serialized layout against the `v: 1` or `v: 2` schema: strict JSON,
 * exactly the expected keys at every level, ids and tabs in range and unique, sizes finite and
 * positive, groups homogeneous (a group's tabs are all chat or all file), a chat group present
 * whenever a file group is, and every cross-reference (`active`, `focused`, `pinned`,
 * and v2's `files`) resolving inside the tree. Anything else, including a parse error or an
 * oversized string, returns `null`. `focusedChat` is never read from the wire; `normalize`
 * derives it fresh (see `panes.ts`). */
export function parseLayout(text: unknown): PaneLayout | null {
  if (typeof text !== "string" || text.length > MAX_LAYOUT_CHARS) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isPlainObject(parsed)) return null;
  const v = parsed.v;
  if (v !== 1 && v !== 2) return null;
  const keys =
    v === 2 ? ["v", "focused", "pinned", "root", "files"] : ["v", "focused", "pinned", "root"];
  if (!exactKeys(parsed, keys)) return null;
  const { focused, pinned, root: rawRoot } = parsed;
  const rawFiles = v === 2 ? parsed.files : undefined;
  if (typeof focused !== "string" || !isStringArray(pinned)) return null;

  const seenIds = new Set<string>();
  const groupIds = new Set<string>();
  const tabs = new Set<string>();
  let nodeCount = 0;
  let sawFileGroup = false;
  let sawChatGroup = false;

  // A group may be empty anywhere in the tree, not just at the root: the chat anchor is an
  // empty non-root group living beside file groups. `normalize` (always run below) is the one
  // that then collapses any looser-than-canonical emptiness back down to at most one such group.
  function parseNode(value: unknown): PaneNode | null {
    nodeCount += 1;
    if (nodeCount > MAX_NODES || !isPlainObject(value)) return null;

    if ("group" in value) {
      if (!exactKeys(value, ["group", "tabs", "active"])) return null;
      const id = value.group;
      const groupTabs = value.tabs;
      const active = value.active;
      if (typeof id !== "string" || !GROUP_ID.test(id) || seenIds.has(id)) return null;
      if (!isStringArray(groupTabs)) return null;
      let fileGroup: boolean | null = null;
      for (const tab of groupTabs) {
        const fileLike = FILE_ID.test(tab);
        if ((!fileLike && !UUID.test(tab)) || tabs.has(tab)) return null;
        if (fileGroup === null) fileGroup = fileLike;
        else if (fileGroup !== fileLike) return null;
        tabs.add(tab);
      }
      if (fileGroup === true) sawFileGroup = true;
      else sawChatGroup = true;
      if (tabs.size > MAX_TABS || groupIds.size >= MAX_PANES) return null;
      if (active === null) {
        // no active tab: always valid
      } else if (typeof active !== "string" || !groupTabs.includes(active)) {
        return null;
      }
      seenIds.add(id);
      groupIds.add(id);
      return { kind: "group", id, tabs: groupTabs, active };
    }

    if ("split" in value) {
      if (!exactKeys(value, ["split", "dir", "sizes", "children"])) return null;
      const id = value.split;
      const dir = value.dir;
      const rawChildren = value.children;
      const sizes = value.sizes;
      if (typeof id !== "string" || !SPLIT_ID.test(id) || seenIds.has(id)) return null;
      if (dir !== "row" && dir !== "column") return null;
      if (!Array.isArray(rawChildren) || rawChildren.length < 2 || rawChildren.length > 4)
        return null;
      if (!isSizes(sizes, rawChildren.length)) return null;
      seenIds.add(id);
      const children: PaneNode[] = [];
      for (const child of rawChildren) {
        const parsedChild = parseNode(child);
        if (!parsedChild) return null;
        children.push(parsedChild);
      }
      return { kind: "split", id, dir, sizes, children };
    }

    return null;
  }

  const root = parseNode(rawRoot);
  if (!root) return null;
  // A layout with a file group must keep a chat group too, even an empty one, never file
  // groups alone. `normalize` below cannot invent a chat group that was never in the wire text,
  // so this is rejected here instead.
  if (sawFileGroup && !sawChatGroup) return null;
  if (new Set(pinned).size !== pinned.length || !pinned.every((tab) => tabs.has(tab))) return null;
  if (!groupIds.has(focused)) return null;

  const files = v === 2 ? parseFiles(rawFiles) : {};
  if (!files) return null;
  for (const tab of tabs) {
    if (FILE_ID.test(tab) && !(tab in files)) return null;
  }
  for (const id of Object.keys(files)) {
    if (!tabs.has(id)) return null;
  }

  return normalize({ root, focused, pinned, files, focusedChat: null });
}
