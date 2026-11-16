/**
 * The vault save lock: `recovery_required` blocks another write to that case-insensitive ref
 * until an explicit recovery status refresh shows the operation/ref has cleared. A `conflict`
 * leaves no lock: the server discards its stage and journal row, and the client re-reads the
 * note instead of blocking it. The exception is a `conflict` with `cleanup_pending: true` (a
 * collision after `prepared`, step 6): its row stays, so it locks exactly as `recovery_required`
 * does. A `publication_busy` answer also locks the ref, with `operationId: null`,
 * since it names no operation to poll. Held here, in a module-level store keyed by
 * `${workspaceId}\0${ref.toLowerCase()}`, so the lock survives VaultBrowser unmount and remount
 * (switching views) and the ref stays scoped to its workspace.
 */
import { useCallback, useSyncExternalStore } from "react";

export interface BlockedWrite {
  operationId: string | null;
  state: "recovery";
}

function storeKey(workspaceId: string, ref: string): string {
  return `${workspaceId}\0${ref.toLowerCase()}`;
}

const locks = new Map<string, BlockedWrite>();
const listeners = new Set<() => void>();
// Bumped on every change; a workspace's cached snapshot is only rebuilt once it is stale, so
// useSyncExternalStore sees the same object identity across renders until something changes.
let version = 0;
const snapshotCache = new Map<
  string,
  { version: number; value: Readonly<Record<string, BlockedWrite>> }
>();

function notify(): void {
  version += 1;
  for (const listener of [...listeners]) listener();
}

export function setBlockedWrite(workspaceId: string, ref: string, value: BlockedWrite): void {
  locks.set(storeKey(workspaceId, ref), value);
  notify();
}

export function clearBlockedWrite(workspaceId: string, ref: string): void {
  if (locks.delete(storeKey(workspaceId, ref))) notify();
}

export function resetBlockedWritesForTests(): void {
  locks.clear();
  snapshotCache.clear();
  notify();
}

function snapshotFor(workspaceId: string): Readonly<Record<string, BlockedWrite>> {
  const cached = snapshotCache.get(workspaceId);
  if (cached && cached.version === version) return cached.value;
  const prefix = `${workspaceId}\0`;
  // Object.create(null) so a New note path of exactly "constructor" or "__proto__" looks up
  // undefined instead of a truthy Object.prototype member.
  const value: Record<string, BlockedWrite> = Object.create(null) as Record<string, BlockedWrite>;
  for (const [key, blocked] of locks)
    if (key.startsWith(prefix)) value[key.slice(prefix.length)] = blocked;
  snapshotCache.set(workspaceId, { version, value });
  return value;
}

/** This workspace's blocked writes, keyed by `ref.toLowerCase()`. */
export function useBlockedWrites(workspaceId: string): Readonly<Record<string, BlockedWrite>> {
  const subscribe = useCallback((onStoreChange: () => void) => {
    listeners.add(onStoreChange);
    return () => listeners.delete(onStoreChange);
  }, []);
  const getSnapshot = useCallback(() => snapshotFor(workspaceId), [workspaceId]);
  return useSyncExternalStore(subscribe, getSnapshot);
}
