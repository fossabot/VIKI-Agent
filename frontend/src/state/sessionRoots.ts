/**
 * Session roots and worktrees as the server lists them, held in memory only. A module store in the
 * pattern of `codeBuffers.ts`: refreshes retain known immutable roots and replace worktrees;
 * `noteRoot` and `noteWorktree` add one row between refreshes.
 */
import { useSyncExternalStore } from "react";
import type { WorkspaceJson } from "../clients/app/shellApi";
import type { RootsApi, SessionRootJson, WorktreeJson } from "../clients/code/rootsApi";

export interface RootsSnapshot {
  roots: ReadonlyMap<string, SessionRootJson>;
  worktrees: readonly WorktreeJson[];
  loaded: boolean;
  /** Missing roots justify the legacy vault fallback only with complete local coverage. */
  rootsComplete: boolean;
}

// Matches session_roots.MAX_ROOT_ROWS; overflow keeps missing identities unknown until reset.
const MAX_ROOT_ROWS = 4096;
let rootsOverflowed = false;
let refreshGeneration = 0;
let snapshot: RootsSnapshot = {
  roots: new Map(),
  worktrees: [],
  loaded: false,
  rootsComplete: false,
};
const listeners = new Set<() => void>();

function set(next: RootsSnapshot): void {
  snapshot = next;
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useRoots(): RootsSnapshot {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** Only the latest non-aborted read publishes; a failure keeps the old snapshot and rethrows. */
export async function refreshRoots(api: RootsApi, signal?: AbortSignal): Promise<void> {
  const generation = ++refreshGeneration;
  const [roots, worktrees] = await Promise.all([api.roots(signal), api.worktrees(signal)]);
  if (generation !== refreshGeneration || signal?.aborted) return;
  const known = new Map(snapshot.roots);
  for (const root of roots.roots) {
    if (known.has(root.session_id)) continue;
    if (known.size >= MAX_ROOT_ROWS) rootsOverflowed = true;
    else known.set(root.session_id, root);
  }
  set({
    roots: known,
    worktrees: worktrees.worktrees,
    loaded: true,
    rootsComplete: !roots.truncated && !rootsOverflowed,
  });
}

export function noteRoot(root: SessionRootJson): void {
  const roots = new Map(snapshot.roots);
  if (roots.has(root.session_id) || roots.size < MAX_ROOT_ROWS) roots.set(root.session_id, root);
  else rootsOverflowed = true;
  set({ ...snapshot, roots, rootsComplete: snapshot.rootsComplete && !rootsOverflowed });
}

/** Inserts the worktree, or replaces the one with its id. */
export function noteWorktree(worktree: WorktreeJson): void {
  const rows = snapshot.worktrees;
  const known = rows.some((row) => row.worktree_id === worktree.worktree_id);
  set({
    ...snapshot,
    worktrees: known
      ? rows.map((row) => (row.worktree_id === worktree.worktree_id ? worktree : row))
      : [worktree, ...rows],
  });
}

/** Clears the store, for tests. */
export function resetRoots(): void {
  refreshGeneration++;
  rootsOverflowed = false;
  set({ roots: new Map(), worktrees: [], loaded: false, rootsComplete: false });
}

/**
 * The folder text for a session with multiple folders, a worktree or a different workspace.
 * Single-folder sessions in the active workspace return null.
 * A removed or unlisted worktree gets a removed suffix.
 */
export function folderLabel(
  sessionId: string,
  roots: RootsSnapshot,
  folders: readonly { workspace_id: string; name: string }[],
  activeWorkspaceId: string | null,
): string | null {
  const root = roots.roots.get(sessionId);
  if (!root) return null;
  const ids = [root.workspace_id, ...(root.additional_workspace_ids ?? [])];
  if (root.worktree_id === null && ids.length === 1 && root.workspace_id === activeWorkspaceId)
    return null;
  const name = ids
    .map((id) => folders.find((folder) => folder.workspace_id === id)?.name ?? "Unknown folder")
    .join(" · ");
  if (root.worktree_id === null) return name;
  const worktree = roots.worktrees.find((row) => row.worktree_id === root.worktree_id);
  const label = worktree ? `${name} · ${worktree.branch}` : name;
  return !worktree || worktree.state === "removed" ? `${label} (removed)` : label;
}

/**
 * The code folder a session's chat should open to: its recorded root's workspace, unless that
 * root sits in a worktree (a worktree is not itself a listed folder); with no recorded root, the
 * workspace currently active in the window. Null while root tracking is on but its first load
 * has not settled, and null whenever the resolved id has no `code` folder with an available root.
 */
export function codeFolderOf(
  sessionId: string,
  roots: RootsSnapshot,
  rootsTracked: boolean,
  folders: readonly WorkspaceJson[],
  activeWorkspaceId: string | null,
): WorkspaceJson | null {
  if (rootsTracked && !roots.loaded) return null;
  const root = roots.roots.get(sessionId);
  const workspaceId = root
    ? root.worktree_id === null
      ? root.workspace_id
      : null
    : activeWorkspaceId;
  return (
    folders.find(
      (folder) =>
        folder.kind === "code" && folder.workspace_id === workspaceId && folder.root_available,
    ) ?? null
  );
}
