import { folderListing } from "../../state/folderListing";
import type { VaultApi, VaultMetadataBatch } from "../../state/vaultApi";
import { fallbackTitle } from "../../state/vaultOverviewStats";

export const BOT_MEMORY_BATCH = 32;
export const BOT_MEMORY_CARD_LIMIT = 5;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MAX_TITLE = 512;
const MAX_REF = 512;

export type BotMemoryDetails =
  | "absent"
  | "unsupported"
  | "truncated"
  | "changed"
  | "unreadable"
  | "unknown"
  | "notRead"
  | null;

export interface BotMemoryRow {
  ref: string;
  title: string;
  modified: number | null;
  details: BotMemoryDetails;
}

export interface BotMemoryListing {
  rows: BotMemoryRow[];
  partial: boolean;
  partialReasons: string[];
  omitted: number;
  metadataPartial: boolean;
  /** The app-kept area's absolute path from the reader; null in vault mode or when not valid. */
  folder: string | null;
}

function validFolder(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 4096 &&
    value.isWellFormed() &&
    !/\p{Cc}/u.test(value) &&
    /^(?:[/\\]|[A-Za-z]:[/\\])/.test(value)
  );
}

function validRef(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    [...value].length <= MAX_REF &&
    !value.startsWith("/") &&
    !value.includes("\\") &&
    !value.includes(":") &&
    !/\p{Cc}/u.test(value) &&
    !value.split("/").some((part) => !part || part === "." || part === "..")
  );
}

function validEntry(value: Record<string, unknown>, prefix: string): boolean {
  if (
    !validRef(value.ref) ||
    !value.ref.startsWith(`${prefix}/`) ||
    !/\.(md|markdown)$/iu.test(value.ref)
  )
    return false;
  if (
    value.title !== undefined &&
    (typeof value.title !== "string" ||
      [...value.title].length > MAX_TITLE ||
      !value.title.isWellFormed() ||
      /\p{Cc}/u.test(value.title))
  )
    return false;
  if (
    value.bytes !== undefined &&
    (typeof value.bytes !== "number" || !Number.isSafeInteger(value.bytes) || value.bytes < 0)
  )
    return false;
  return (
    value.modified === undefined ||
    (typeof value.modified === "number" && Number.isFinite(value.modified))
  );
}

function legacyTitle(value: unknown): string | null {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    !value.isWellFormed() ||
    [...value].length > MAX_TITLE ||
    /\p{Cc}/u.test(value) ||
    !/\P{White_Space}/u.test(value) ||
    /^(skill|readme|index)(?:\.(?:md|markdown))?$/iu.test(value)
  )
    return null;
  return value;
}

function checkCurrent(signal: AbortSignal | undefined, isCurrent: () => boolean): void {
  if (signal?.aborted || !isCurrent()) throw new Error("cancelled");
}

/** Read one Bot's requested folder, then enrich no more than one bounded prefix batch. */
export async function listBotMemory(
  api: Pick<VaultApi, "entries" | "metadata">,
  workspaceId: string,
  botId: string,
  signal?: AbortSignal,
  isCurrent: () => boolean = () => true,
  kept: "vault" | "app" = "vault",
): Promise<BotMemoryListing> {
  if (!UUID.test(workspaceId) || !UUID.test(botId)) throw new Error("invalid_identity");
  const prefix = kept === "app" ? botId : `bot-memory/${botId}`;
  const raw = await api.entries(workspaceId, undefined, signal);
  checkCurrent(signal, isCurrent);
  const listing = folderListing(raw, prefix, false);
  const rows = listing.entries.map((entry) => {
    if (!validEntry(entry, prefix)) throw new Error("invalid_response");
    return {
      ref: entry.ref as string,
      title: legacyTitle(entry.title) ?? fallbackTitle(entry.ref as string),
      modified: (entry.modified as number | undefined) ?? null,
      details: null as BotMemoryDetails,
    };
  });
  if (new Set(rows.map((row) => row.ref)).size !== rows.length) throw new Error("invalid_response");
  if (!api.metadata) for (const row of rows) row.details = "notRead";
  rows.sort((left, right) => {
    const leftIndex = left.ref === `${prefix}/MEMORY.md`;
    const rightIndex = right.ref === `${prefix}/MEMORY.md`;
    return (
      Number(rightIndex) - Number(leftIndex) ||
      (right.modified ?? 0) - (left.modified ?? 0) ||
      (left.ref < right.ref ? -1 : left.ref > right.ref ? 1 : 0)
    );
  });

  const refs = rows.slice(0, BOT_MEMORY_BATCH).map((row) => row.ref);
  let metadataPartial = false;
  if (api.metadata && refs.length > 0) {
    // VaultApi.metadata has already validated the batch against the requested refs.
    let enriched: VaultMetadataBatch | null = null;
    try {
      enriched = await api.metadata(workspaceId, refs, signal);
      checkCurrent(signal, isCurrent);
    } catch (error) {
      if (error instanceof Error && error.message === "invalid_response") throw error;
      checkCurrent(signal, isCurrent);
      metadataPartial = true;
    }
    if (enriched) {
      metadataPartial = enriched.partial;
      for (const [index, item] of enriched.items.entries()) {
        const row = rows[index];
        if (!row) continue;
        const title = item.display_metadata?.display_title;
        row.title = title ?? legacyTitle(row.title) ?? fallbackTitle(row.ref);
        row.details = item.state === "ready" ? null : item.state;
      }
    } else {
      for (const row of rows.slice(0, BOT_MEMORY_BATCH)) row.details = "unknown";
    }
  }
  for (const row of rows.slice(BOT_MEMORY_BATCH)) row.details = "notRead";
  checkCurrent(signal, isCurrent);
  return {
    rows,
    partial: listing.partial,
    partialReasons: listing.partial_reasons,
    omitted: listing.omitted,
    metadataPartial,
    folder: kept === "app" && validFolder(raw.folder) ? raw.folder : null,
  };
}

export function botMemoryCard(listing: BotMemoryListing): {
  rows: BotMemoryRow[];
  remaining: number;
} {
  return {
    rows: listing.rows.slice(0, BOT_MEMORY_CARD_LIMIT),
    remaining: Math.max(0, listing.rows.length - BOT_MEMORY_CARD_LIMIT),
  };
}
