/**
 * Unsaved edits in the Vault and Agents views. Leaving either view unmounts its editor
 * and drops the edit, so each view reports whether it holds one, and App asks before any
 * navigation away and blocks `beforeunload` while one is held. A module store read through
 * `useSyncExternalStore`, in the pattern of `vaultWriteLocks.ts`: App renders the views, so a
 * callback prop would have to thread through every place App renders them.
 */
import { useEffect, useSyncExternalStore } from "react";

export type UnsavedWorkOwner = "vault" | "profile" | "routine" | "code" | "instructions" | "memory";

const owners = new Set<UnsavedWorkOwner>();
const listeners = new Set<() => void>();

export function setUnsavedWork(owner: UnsavedWorkOwner, dirty: boolean): void {
  if (dirty === owners.has(owner)) return;
  if (dirty) owners.add(owner);
  else owners.delete(owner);
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Read at the moment of a navigation, so the answer is never a render behind. Code buffers are
 * not counted: they live outside the views and survive a view change, so leaving one asks nothing.
 * Nor is a Bot memory edit: its modal dialog asks before it closes, and its Save leaves the page
 * on purpose once the text is in a draft.
 */
export function hasUnsavedWork(): boolean {
  return [...owners].some((owner) => owner !== "code" && owner !== "memory");
}

/** The unload guard counts every owner, code buffers included: closing the window drops them. */
export function hasAnyUnsavedWork(): boolean {
  return owners.size > 0;
}

export function useHasUnsavedWork(): boolean {
  return useSyncExternalStore(subscribe, hasAnyUnsavedWork);
}

/** A view reports its `dirty` while mounted; unmounting takes the edit with it, so it clears. */
export function useReportUnsavedWork(owner: UnsavedWorkOwner, dirty: boolean): void {
  useEffect(() => {
    setUnsavedWork(owner, dirty);
    return () => setUnsavedWork(owner, false);
  }, [owner, dirty]);
}
