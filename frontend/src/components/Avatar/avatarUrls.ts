/** The App-level avatar object-URL cache: one fetch per distinct `(id, size)`,
 * shared by every holder and revoked once the last one releases. A release that lands before the
 * fetch settles still revokes the URL when it later arrives, so a fast unmount never leaks one. */
import { useEffect, useState } from "react";
import type { AvatarSize, AvatarsApi } from "../../clients/media/avatarsApi";

const SIZES: readonly AvatarSize[] = [24, 32, 64, 128, 256];

export interface AvatarUrls {
  acquire(id: string, size: AvatarSize, onChange: () => void): () => void;
  get(id: string, size: AvatarSize): string | null | "failed";
  dispose(): void;
}

interface Entry {
  holders: number;
  controller: AbortController;
  state: string | null | "failed";
  listeners: Set<() => void>;
  /** Set once the entry is removed (last release or dispose): a fetch that resolves after this
   * revokes its own URL instead of publishing it. */
  cancelled: boolean;
}

function notify(entry: Entry): void {
  for (const listener of entry.listeners) listener();
}

function revokeIfLoaded(state: Entry["state"]): void {
  if (typeof state === "string" && state !== "failed") URL.revokeObjectURL(state);
}

export function createAvatarUrls(api: Pick<AvatarsApi, "variant">): AvatarUrls {
  const entries = new Map<string, Entry>();

  function acquire(id: string, size: AvatarSize, onChange: () => void): () => void {
    const key = `${id}:${size}`;
    let entry = entries.get(key);
    if (!entry) {
      const controller = new AbortController();
      entry = { holders: 0, controller, state: null, listeners: new Set(), cancelled: false };
      const created = entry;
      entries.set(key, created);
      void api.variant(id, size, controller.signal).then(
        (blob) => {
          const url = URL.createObjectURL(blob);
          if (created.cancelled) {
            URL.revokeObjectURL(url);
            return;
          }
          created.state = url;
          notify(created);
        },
        () => {
          if (created.cancelled) return;
          created.state = "failed";
          notify(created);
        },
      );
    }
    entry.holders += 1;
    entry.listeners.add(onChange);
    return () => {
      entry.listeners.delete(onChange);
      entry.holders -= 1;
      if (entry.holders <= 0) {
        entries.delete(key);
        entry.cancelled = true;
        entry.controller.abort();
        revokeIfLoaded(entry.state);
      }
    };
  }

  function get(id: string, size: AvatarSize): string | null | "failed" {
    return entries.get(`${id}:${size}`)?.state ?? null;
  }

  function dispose(): void {
    for (const entry of entries.values()) {
      entry.cancelled = true;
      entry.controller.abort();
      revokeIfLoaded(entry.state);
    }
    entries.clear();
  }

  return { acquire, get, dispose };
}

/** Acquires `(id, size)` for the component's lifetime, releasing on every change and on unmount;
 * re-renders when the cache notifies a state change. */
export function useAvatarUrl(
  urls: AvatarUrls | null,
  id: string | null,
  size: AvatarSize,
): { url: string | null; failed: boolean } {
  const [, bump] = useState(0);
  useEffect(() => {
    if (!urls || !id) return;
    return urls.acquire(id, size, () => bump((n) => n + 1));
  }, [urls, id, size]);
  if (!urls || !id) return { url: null, failed: false };
  const value = urls.get(id, size);
  const failed = value === "failed";
  return { url: !failed && typeof value === "string" ? value : null, failed };
}

/** The smallest served size at least `px`, else the largest (256). */
export function variantFor(px: number): AvatarSize {
  for (const size of SIZES) if (size >= px) return size;
  return 256;
}
