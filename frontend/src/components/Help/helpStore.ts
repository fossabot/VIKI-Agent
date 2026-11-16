/**
 * Help overlay state: which help target is open, if any, and how many host components (the Help
 * center dialog) are mounted to show it. The host installs `useHelpShortcut` for the bare `?`
 * key. A module store in the pattern of `state/sessionRoots.ts`.
 */
import { useEffect, useSyncExternalStore } from "react";

export type HelpTopicId =
  | "welcome"
  | "getting-started"
  | "chats"
  | "approvals"
  | "sources"
  | "vault"
  | "code"
  | "agents"
  | "decision"
  | "routines"
  | "messaging"
  | "voice"
  | "settings"
  | "data"
  | "keyboard-shortcuts"
  | "troubleshooting"
  | "glossary";

export type HelpTarget =
  | { kind: "topic"; id: HelpTopicId }
  | { kind: "search"; query: string }
  | { kind: "tour" }
  | { kind: "whats-new" };

interface HelpSnapshot {
  target: HelpTarget | null;
  hosts: number;
}

let snapshot: HelpSnapshot = { target: null, hosts: 0 };
const listeners = new Set<() => void>();

function set(next: HelpSnapshot): void {
  snapshot = next;
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Opens the help overlay at this target, replacing whatever was open. */
export function openHelp(target: HelpTarget): void {
  set({ ...snapshot, target });
}

/** Closes the help overlay, if open. */
export function closeHelp(): void {
  set({ ...snapshot, target: null });
}

export function useHelpTarget(): HelpTarget | null {
  return useSyncExternalStore(subscribe, () => snapshot.target);
}

/** Registers the calling component as a mounted help host for as long as it stays mounted. One
 * or more hosts is what `useHelpAvailable()` reports elsewhere in the tree. */
export function useHelpHost(): void {
  useEffect(() => {
    set({ ...snapshot, hosts: snapshot.hosts + 1 });
    return () => set({ ...snapshot, hosts: snapshot.hosts - 1 });
  }, []);
}

export function useHelpAvailable(): boolean {
  return useSyncExternalStore(subscribe, () => snapshot.hosts > 0);
}

/** Clears the store, for tests. */
export function resetHelp(): void {
  set({ target: null, hosts: 0 });
}

/** True for a keydown target that takes typed text: an input, a textarea, a select or a
 * contenteditable element — copies App.tsx's own `isTextEntryTarget`, plus the select case its
 * Escape guard adds separately, since the `?` shortcut must stay out of every typing surface. */
function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" ||
    target.isContentEditable
  );
}

/**
 * Opens the keyboard-shortcuts topic on a bare `?`, mirroring App.tsx's own window keydown
 * guards: nothing while a modifier is held, while composing (the IME guard App.tsx and this
 * codebase use throughout: `isComposing` or the `keyCode === 229` some IME/browser pairs send
 * instead), once another handler has already claimed the key, while a dialog is open, or while
 * the target takes typed text.
 */
export function useHelpShortcut(): void {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key !== "?") return;
      if (event.ctrlKey || event.altKey || event.metaKey) return;
      if (event.isComposing || event.keyCode === 229) return;
      if (event.defaultPrevented) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      if (isTypingTarget(event.target)) return;
      event.preventDefault();
      openHelp({ kind: "topic", id: "keyboard-shortcuts" });
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
