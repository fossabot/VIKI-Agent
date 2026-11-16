/**
 * The vault-facing environment a source chip needs to act: open a note, folder or URL, peek a
 * note's frontmatter facts, resolve a search/web chip's exact target, and save a turn as a note.
 * Published once the app wires up the active vault, and read by every chip
 * through `useSourcesEnv()`; `null` until then, so a chip can render safely before that happens.
 * A module store in the pattern of `components/Help/helpStore.ts`.
 */
import { useSyncExternalStore } from "react";
import type { ToolSource } from "../../clients/conversations/conversationSearchApi";
import type { NotePeek } from "../../state/notePeek";
import type { Turn } from "../../state/view";

export interface SourcesEnv {
  /** Exact active workspace; explicit undefined for a rootless/manual environment. */
  workspaceId: string | undefined;
  /** Whether this session is backed by a vault at all. */
  inVault(sid: string): boolean;
  /** Opens the Vault view at this note (a workspace-relative `.md`/`.markdown` path). */
  openNote(ref: string): void;
  /** Opens the Vault view at this folder (`""` for the vault root). */
  openFolder(folder: string): void;
  /** Opens this exact URL in the system browser. */
  openUrl(url: string): void;
  /** Reads one note's title, summary, modified and confidence facts for a chip's hover card. */
  peek?(ref: string, signal?: AbortSignal): Promise<NotePeek>;
  /** Looks up the real tool sources (a fetched URL, or a searched path) behind tool-use ids. */
  resolve?(
    sid: string,
    ids: readonly string[],
    signal?: AbortSignal,
  ): Promise<ReadonlyMap<string, ToolSource>>;
  /** Saves a turn's reply as a new note. */
  saveAsNote?(sid: string, turn: Turn): Promise<"opened" | "no_room" | "unavailable">;
}

let snapshot: SourcesEnv | null = null;
const listeners = new Set<() => void>();

function set(next: SourcesEnv | null): void {
  snapshot = next;
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Publishes the environment every source chip reads through `useSourcesEnv`; `null` tears it
 * down (e.g. the active chat leaves its vault). */
export function publishSourcesEnv(env: SourcesEnv | null): void {
  set(env);
}

export function useSourcesEnv(): SourcesEnv | null {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** Clears the store, for tests. */
export function resetSourcesEnv(): void {
  set(null);
}
