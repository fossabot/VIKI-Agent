/**
 * A turn's sources: the vault notes VIKI read, the folders it searched and the pages it fetched,
 * derived from the turn's own blocks — the same rows the transcript already shows (walks blocks
 * the same way RetryReview.tsx's retryFlags does). No network, no new state: a chip resolves its
 * exact open target later, keyed by toolUseId.
 */
import type { ToolRowView, Turn } from "./view";

export const NOTE_PATH = /\.(md|markdown)$/i;
const SPECIAL_STEMS = new Set(["skill", "readme", "index"]);
const SEARCH_TOOLS = new Set(["Grep", "Glob"]);
const CHANGED_TOOLS = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit"]);

export interface Source {
  kind: "note" | "search" | "web";
  key: string;
  label: string;
  ref: string;
  toolUseId: string;
  tool: string;
  agent: string;
}

/** A note's display label: its filename without the extension, unless that stem is SKILL, README
 * or index (any case) — those are named for their folder instead, since the bare stem alone reads
 * the same across every skill or section. A root-level file has no folder to add. */
export function noteLabel(ref: string): string {
  const segments = ref.split("/");
  const filename = segments[segments.length - 1] ?? "";
  const stem = filename.replace(NOTE_PATH, "");
  const parent = segments.length > 1 ? segments[segments.length - 2] : "";
  return parent && SPECIAL_STEMS.has(stem.toLowerCase()) ? `${parent}/${stem}` : stem;
}

/** One row's source, or null when it is not a successful Read of a vault note, a non-empty
 * Grep/Glob, or a non-empty WebFetch. `vault` gates note and search; a web fetch counts from any
 * session. */
function sourceFromRow(row: ToolRowView, agent: string, vault: boolean): Source | null {
  if (!row.done || row.isError || row.artifactResult?.observation === "error") return null;
  const start = row.artifactStart;
  if (
    vault &&
    row.tool === "Read" &&
    start &&
    start.operation === "read" &&
    start.path_state === "workspace_relative" &&
    NOTE_PATH.test(start.path)
  ) {
    const ref = start.path;
    return {
      kind: "note",
      key: `note:${ref}`,
      label: noteLabel(ref),
      ref,
      toolUseId: row.id,
      tool: row.tool,
      agent,
    };
  }
  const rest = row.row.rest;
  if (vault && SEARCH_TOOLS.has(row.tool) && rest !== "") {
    return {
      kind: "search",
      key: `search:${row.tool}:${rest}`,
      label: rest,
      ref: "",
      toolUseId: row.id,
      tool: row.tool,
      agent,
    };
  }
  if (row.tool === "WebFetch" && rest !== "") {
    return {
      kind: "web",
      key: `web:${rest.toLowerCase()}`,
      label: rest,
      ref: "",
      toolUseId: row.id,
      tool: row.tool,
      agent,
    };
  }
  return null;
}

/** Keeps the first source for each key, in the order given. */
function dedupeByKey(sources: readonly Source[]): Source[] {
  const seen = new Set<string>();
  const out: Source[] = [];
  for (const source of sources) {
    if (seen.has(source.key)) continue;
    seen.add(source.key);
    out.push(source);
  }
  return out;
}

/** A turn's sources in arrival order, deduplicated by key. Walks the same two block kinds as
 * RetryReview.tsx's retryFlags: a "tools" block's own rows (agent ""), and a "routing" block's
 * nested rows (agent = the subagent's name) — the routing block itself is never a row. */
export function turnSources(turn: Turn, vault: boolean): Source[] {
  const candidates: Source[] = [];
  for (const block of turn.blocks) {
    if (block.kind === "tools") {
      for (const row of block.rows) {
        const source = sourceFromRow(row, "", vault);
        if (source) candidates.push(source);
      }
    } else if (block.kind === "routing") {
      for (const row of block.tools) {
        const source = sourceFromRow(row, block.agent, vault);
        if (source) candidates.push(source);
      }
    }
  }
  return dedupeByKey(candidates);
}

export type DigestGroup = "changed" | "read" | "web" | "search";

export interface DigestEntry {
  key: string;
  path: string | null;
  label: string;
  count: number;
  agents: string[];
  viki: boolean;
  source: Source | null;
}

export type ChatDigest = Record<DigestGroup, DigestEntry[]>;

interface Classified {
  group: DigestGroup;
  dedupe: string;
  label: string;
  path: string | null;
  source: Source | null;
}

/** One row's digest bucket, or null when it is not a successful tool call (same gate as
 * `sourceFromRow`'s first guard) in a group this digest tracks. Changed/Read keep the row's
 * resolved workspace path when there is one, falling back to its plain-text `rest`; Web and
 * Search keep `rest` itself. A search row's `source` is set only under `vault`, since a search
 * result resolves against the vault the same way a note read does; a web fetch's `source` is
 * always set, matching `sourceFromRow`. */
function classifyRow(row: ToolRowView, vault: boolean): Classified | null {
  if (!row.done || row.isError || row.artifactResult?.observation === "error") return null;
  const rest = row.row.rest;
  if (row.tool === "WebFetch") {
    if (rest === "") return null;
    const dedupe = rest.toLowerCase();
    return {
      group: "web",
      dedupe,
      label: rest,
      path: null,
      source: {
        kind: "web",
        key: `web:${dedupe}`,
        label: rest,
        ref: "",
        toolUseId: row.id,
        tool: row.tool,
        agent: "",
      },
    };
  }
  if (SEARCH_TOOLS.has(row.tool)) {
    if (rest === "") return null;
    const dedupe = `${row.tool}:${rest}`;
    return {
      group: "search",
      dedupe,
      label: rest,
      path: null,
      source: vault
        ? {
            kind: "search",
            key: `search:${dedupe}`,
            label: rest,
            ref: "",
            toolUseId: row.id,
            tool: row.tool,
            agent: "",
          }
        : null,
    };
  }
  const changed = CHANGED_TOOLS.has(row.tool);
  if (!changed && row.tool !== "Read") return null;
  const start = row.artifactStart;
  const path =
    start && start.path_state === "workspace_relative" && start.path !== "" ? start.path : null;
  const label = path ?? rest;
  if (label === "") return null;
  return { group: changed ? "changed" : "read", dedupe: label, label, path, source: null };
}

/** A turn's tool rows in chronological order, the same two block kinds and agent attribution as
 * `turnSources` — a "tools" block's own rows (agent ""), a "routing" block's nested rows
 * (agent = the subagent's name) — but every row kept, not just the ones that become a `Source`. */
function turnRows(turn: Turn): { row: ToolRowView; agent: string }[] {
  const rows: { row: ToolRowView; agent: string }[] = [];
  for (const block of turn.blocks) {
    if (block.kind === "tools") {
      for (const row of block.rows) rows.push({ row, agent: "" });
    } else if (block.kind === "routing") {
      for (const row of block.tools) rows.push({ row, agent: block.agent });
    }
  }
  return rows;
}

/** The chat's files digest: every changed, read, searched or fetched path, newest touch first,
 * grouped and folded. Walks turns newest first and, within a turn, rows newest first (the exact
 * reverse of `turnRows`' chronological order), so the first sighting of a key is always its
 * newest row. A repeat sighting only grows the count and the agent list — `viki` stays true once
 * any sighting (first or repeat) had no agent (the main thread, not a subagent). */
export function chatDigest(turns: readonly Turn[], vault: boolean): ChatDigest {
  const grouped: ChatDigest = { changed: [], read: [], web: [], search: [] };
  const byKey = new Map<string, DigestEntry>();

  for (const turn of [...turns].reverse()) {
    for (const { row, agent } of [...turnRows(turn)].reverse()) {
      const classified = classifyRow(row, vault);
      if (!classified) continue;
      const key = `${classified.group}:${classified.dedupe}`;
      const existing = byKey.get(key);
      if (existing) {
        existing.count += 1;
        if (agent !== "" && !existing.agents.includes(agent)) existing.agents.push(agent);
        existing.viki = existing.viki || agent === "";
        continue;
      }
      const entry: DigestEntry = {
        key,
        path: classified.path,
        label: classified.label,
        count: 1,
        agents: agent === "" ? [] : [agent],
        viki: agent === "",
        source: classified.source,
      };
      byKey.set(key, entry);
      grouped[classified.group].push(entry);
    }
  }

  return grouped;
}
