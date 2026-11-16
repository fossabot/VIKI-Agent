/**
 * Unsaved or unsettled code edits, held in memory only (nothing is written to storage). A module
 * store in the pattern of `vaultWriteLocks.ts`, keyed by `${workspaceId}\0${ref}`, the exact ref: a
 * case-sensitive folder can hold `A.txt` and `a.txt`, and one file reached under two spellings gets
 * two buffers whose second save conflicts rather than overwrites. A buffer survives the editor
 * unmounting when the view or the open file changes. While any buffer is held the `code` owner of
 * `unsavedWork.ts` is set, so closing the window asks first. Under the same key it records which
 * view holds the window's one editor of a file, and counts the file's saves, so a view that is not
 * editing the file can read it again after a save elsewhere.
 */
import { useSyncExternalStore } from "react";
import type { CodeSaveBody } from "../clients/code/codeApi";
import { setUnsavedWork } from "./unsavedWork";

/** The file as last read or saved: what the buffer is compared and saved against. */
export interface CodeBufferBase {
  text: string;
  sha256: string;
  encoding: CodeSaveBody["encoding"];
  newline: CodeSaveBody["newline"];
}

export interface CodeBuffer {
  workspaceId: string;
  ref: string;
  base: CodeBufferBase;
  text: string;
}

interface BufferRecord {
  buffer: CodeBuffer;
  pending: Set<symbol>;
  uncertain: boolean;
}

const buffers = new Map<string, BufferRecord>();
const owners = new Map<string, string>();
const saves = new Map<string, number>();
const listeners = new Set<() => void>();
const baseListeners = new Set<(buffer: CodeBuffer, previousSha256: string) => void>();
let version = 0;

function storeKey(workspaceId: string, ref: string): string {
  return `${workspaceId}\0${ref}`;
}

function toLf(text: string): string {
  return text.replace(/\r\n?/g, "\n");
}

function notify(): void {
  version += 1;
  setUnsavedWork("code", buffers.size > 0);
  for (const listener of [...listeners]) listener();
}

export function getBuffer(workspaceId: string, ref: string): CodeBuffer | undefined {
  return buffers.get(storeKey(workspaceId, ref))?.buffer;
}

/** A pending or unheard save can have changed the base even when the text now matches the old one. */
export function putBuffer(buffer: CodeBuffer): void {
  const key = storeKey(buffer.workspaceId, buffer.ref);
  const current = buffers.get(key);
  if (toLf(buffer.text) !== toLf(buffer.base.text) || current?.pending.size || current?.uncertain) {
    if (current) current.buffer = buffer;
    else buffers.set(key, { buffer, pending: new Set(), uncertain: false });
  } else if (!buffers.delete(key)) return;
  notify();
}

/** A different verified hash rules out a pending write against the old base. The same hash cannot. */
export function confirmBufferBase(buffer: CodeBuffer): void {
  const current = buffers.get(storeKey(buffer.workspaceId, buffer.ref));
  if (current && current.buffer.base.sha256 !== buffer.base.sha256) current.uncertain = false;
  putBuffer(buffer);
}

/** Observes an accepted save base, including when the now-clean buffer is removed. */
export function subscribeBufferBase(
  workspaceId: string,
  ref: string,
  listener: (base: CodeBufferBase, previousSha256: string) => void,
): () => void {
  const accept = (buffer: CodeBuffer, previousSha256: string) => {
    if (buffer.workspaceId === workspaceId && buffer.ref === ref) {
      listener(buffer.base, previousSha256);
    }
  };
  baseListeners.add(accept);
  return () => baseListeners.delete(accept);
}

/** Holds this buffer owner through a save. No base means the result was not verified. */
export function beginBufferSave(buffer: CodeBuffer) {
  const key = storeKey(buffer.workspaceId, buffer.ref);
  const owner = buffers.get(key) ?? { buffer, pending: new Set<symbol>(), uncertain: false };
  const token = Symbol();
  owner.buffer = buffer;
  owner.pending.add(token);
  buffers.set(key, owner);
  notify();
  return (base?: CodeBufferBase, text?: string): boolean => {
    // Explicit discard invalidates this owner, including when a new edit reuses its exact ref.
    if (buffers.get(key) !== owner || !owner.pending.delete(token)) return false;
    const ownsBase = owner.buffer.base.sha256 === buffer.base.sha256;
    if (base && ownsBase) {
      owner.buffer = { ...owner.buffer, base, text: text ?? owner.buffer.text };
      owner.uncertain = false;
    } else if (!base) {
      owner.uncertain = true;
    }
    putBuffer(owner.buffer);
    if (base && ownsBase) {
      for (const listener of [...baseListeners]) listener(owner.buffer, buffer.base.sha256);
    }
    return base !== undefined && ownsBase;
  };
}

export function dropBuffer(workspaceId: string, ref: string): void {
  if (buffers.delete(storeKey(workspaceId, ref))) notify();
}

/** The refs with an unsaved or unsettled buffer in this workspace, sorted. */
export function dirtyRefs(workspaceId: string): string[] {
  return [...buffers.values()]
    .filter(({ buffer }) => buffer.workspaceId === workspaceId)
    .map(({ buffer }) => buffer.ref)
    .sort();
}

/** Takes the file's editor for `owner`: true when it was free or `owner` already holds it. */
export function claimEditor(workspaceId: string, ref: string, owner: string): boolean {
  const key = storeKey(workspaceId, ref);
  const holder = owners.get(key);
  if (holder !== undefined) return holder === owner;
  owners.set(key, owner);
  notify();
  return true;
}

/** Frees the file's editor when `owner` holds it; another owner's release changes nothing. */
export function releaseEditor(workspaceId: string, ref: string, owner: string): void {
  const key = storeKey(workspaceId, ref);
  if (owners.get(key) !== owner) return;
  owners.delete(key);
  notify();
}

export function editorOwner(workspaceId: string, ref: string): string | undefined {
  return owners.get(storeKey(workspaceId, ref));
}

/** Counts a save of the file, which its views that are not editing it read as a change. */
export function noteSaved(workspaceId: string, ref: string): void {
  saves.set(storeKey(workspaceId, ref), savedVersion(workspaceId, ref) + 1);
  notify();
}

export function savedVersion(workspaceId: string, ref: string): number {
  return saves.get(storeKey(workspaceId, ref)) ?? 0;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** A version number that changes whenever a buffer, an editor's owner or a save count changes, for
 * a component to re-render on. */
export function useCodeBuffers(): number {
  return useSyncExternalStore(subscribe, () => version);
}
