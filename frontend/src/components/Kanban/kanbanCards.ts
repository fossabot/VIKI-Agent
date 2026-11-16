import { folderListing } from "../../state/folderListing";
import { metadataRef, type VaultMetadataItem } from "../../state/vaultApi";
import { fallbackTitle } from "../../state/vaultOverviewStats";

export const KANBAN_FOLDER = "kanban";
export const KANBAN_BATCH = 32;
export const KANBAN_READ_CAP = 128;

export const STALE_WORKING_DAYS = 1; // contracts.CARD_STALE_WORKING (pinned by tests/test_kanban_names.py)
export const STALE_TODO_DAYS = 3; // contracts.CARD_STALE_TODO

export interface KanbanListing {
  refs: string[];
  modified: ReadonlyMap<string, number>;
  partial: boolean;
  reasons: string[];
  omitted: number;
}

export function kanbanListing(body: Record<string, unknown>): KanbanListing {
  const {
    entries,
    partial,
    partial_reasons: reasons,
    omitted,
  } = folderListing(body, KANBAN_FOLDER, true);
  const refs: string[] = [];
  const modified = new Map<string, number>();
  for (const entry of entries) {
    const { ref, modified: time } = entry;
    if (typeof ref !== "string") throw new Error("invalid_response");
    if (time !== undefined) {
      if (typeof time !== "number" || !Number.isFinite(time)) throw new Error("invalid_response");
      modified.set(ref, time);
    }
    refs.push(ref);
  }
  return { refs, modified, partial, reasons: reasons as string[], omitted };
}

export function kanbanReadRefs(refs: readonly string[]): string[] {
  return refs.filter(metadataRef).slice(0, KANBAN_READ_CAP);
}

export type KanbanLaneId = "todo" | "working" | "blocked" | "review" | "done" | "other" | "unread";
export type UnreadCode =
  | "unsupported"
  | "truncated"
  | "changed"
  | "unreadable"
  | "stopped"
  | "retry"
  | "name"
  | "cap";
export interface KanbanCard {
  ref: string;
  title: string;
  owners: string[];
  status: string | null;
  ageDays: number | null;
  stale: boolean;
  unread: { code: UnreadCode; reason: string | null } | null;
}
export interface KanbanLane {
  id: KanbanLaneId;
  cards: KanbanCard[];
}

const STATUS_LANES = new Map<string | null, KanbanLaneId>(
  (["todo", "working", "blocked", "review", "done"] as const).map((id) => [id, id]),
);
const DAY_MS = 86_400_000;
const STALE_AFTER_MS: Record<string, number> = {
  working: STALE_WORKING_DAYS * DAY_MS,
  todo: STALE_TODO_DAYS * DAY_MS,
};

function localDay(ms: number): number {
  const d = new Date(ms);
  return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / DAY_MS;
}

function owners(tags: readonly string[]): string[] {
  return tags.filter((tag) => tag.startsWith("owner/") && tag.length > 6).map((t) => t.slice(6));
}

function unreadOf(
  ref: string,
  item: VaultMetadataItem | undefined,
  readRefs: readonly string[],
): { code: UnreadCode; reason: string | null } {
  const front = item?.display_metadata?.frontmatter_state;
  if (front === "unsupported") return { code: "unsupported", reason: null };
  if (front === "truncated") return { code: "truncated", reason: null };
  if (item?.state === "changed") return { code: "changed", reason: null };
  if (item?.state === "unreadable") return { code: "unreadable", reason: item.reason };
  if (item?.state === "unknown") return { code: "stopped", reason: item.reason };
  if (item) return { code: "retry", reason: null };
  if (!metadataRef(ref)) return { code: "name", reason: null };
  if (!readRefs.includes(ref)) return { code: "cap", reason: null };
  return { code: "retry", reason: null };
}

export function kanbanLanes(
  listing: KanbanListing,
  items: ReadonlyMap<string, VaultMetadataItem>,
  nowMs: number,
): KanbanLane[] {
  const readRefs = kanbanReadRefs(listing.refs);
  const cards = new Map<KanbanLaneId, KanbanCard[]>();
  for (const ref of listing.refs) {
    const item = items.get(ref);
    const meta = item?.display_metadata ?? null;
    const readable =
      meta && (meta.frontmatter_state === "ready" || meta.frontmatter_state === "absent");
    const status = readable ? meta.status : null;
    const time = listing.modified.get(ref);
    const card: KanbanCard = {
      ref,
      title: meta?.display_title ?? fallbackTitle(ref),
      owners: readable ? owners(meta.tags ?? []) : [],
      status,
      ageDays: time === undefined ? null : Math.max(0, localDay(nowMs) - localDay(time * 1000)),
      stale:
        time !== undefined &&
        status !== null &&
        Object.hasOwn(STALE_AFTER_MS, status) &&
        nowMs - time * 1000 >= (STALE_AFTER_MS[status] ?? Number.POSITIVE_INFINITY),
      unread: readable ? null : unreadOf(ref, item, readRefs),
    };
    const id: KanbanLaneId = !readable ? "unread" : (STATUS_LANES.get(status) ?? "other");
    const lane = cards.get(id);
    if (lane) lane.push(card);
    else cards.set(id, [card]);
  }
  const ids: KanbanLaneId[] = ["todo", "working", "blocked", "review", "done", "other", "unread"];
  return ids
    .filter((id) => (id !== "other" && id !== "unread") || (cards.get(id)?.length ?? 0) > 0)
    .map((id) => ({ id, cards: cards.get(id) ?? [] }));
}

export function kanbanAttention(
  listing: KanbanListing,
  lanes: readonly KanbanLane[],
): { review: number; blocked: number } | null {
  const count = (id: KanbanLaneId) => lanes.find((l) => l.id === id)?.cards.length ?? 0;
  if (
    listing.partial ||
    listing.omitted !== 0 ||
    listing.refs.length > KANBAN_READ_CAP ||
    count("unread") > 0
  )
    return null;
  return { review: count("review"), blocked: count("blocked") };
}
