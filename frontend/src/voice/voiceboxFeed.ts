/**
 * A `VoiceboxFeed`: text pushed in, speech bytes out, one request ahead of playback (`AudioFeed`).
 *
 * `push` queues text for `speak`. Every queued chunk's group is always <= the highest group seen,
 * so a push whose group is strictly higher than that proves every chunk still queued is of a lower
 * group — the whole queue is dropped, and the new chunk starts the next one. `next` drains the
 * queue in order, waiting for a push, an `end`, or its own abort when the queue runs dry.
 *
 * `startAudio` starts a `playAudio` from this feed's first chunk, aborting the wait once the
 * controller says the ticket has gone stale.
 */
import type { AudioFeed, ReadAloud } from "./readAloud";
import { VoiceboxError } from "./voiceboxApi";

export interface VoiceboxFeed extends AudioFeed {
  push(text: string, group?: number): void;
  end(): void;
  /** The first recorded `speak` failure's code, or null while none has failed. */
  failure(): string | null;
}

function abortError(): DOMException {
  return new DOMException("Voicebox feed aborted", "AbortError");
}

export function createVoiceboxFeed(
  speak: (text: string, signal: AbortSignal) => Promise<ArrayBuffer>,
): VoiceboxFeed {
  const queue: { text: string; group: number }[] = [];
  let ended = false;
  let highestGroup = 0;
  let failureCode: string | null = null;
  let waiter: (() => void) | null = null;

  function wake() {
    const woken = waiter;
    waiter = null;
    woken?.();
  }

  function push(text: string, group = 0): void {
    if (ended || text.trim() === "") return;
    if (group > highestGroup) {
      queue.length = 0;
      highestGroup = group;
    }
    queue.push({ text, group });
    wake();
  }

  function end(): void {
    ended = true;
    wake();
  }

  function failure(): string | null {
    return failureCode;
  }

  function next(signal: AbortSignal): Promise<ArrayBuffer | null> {
    if (signal.aborted) return Promise.reject(abortError());
    const item = queue.shift();
    if (item !== undefined) {
      return speak(item.text, signal).catch((error) => {
        if (!signal.aborted && failureCode === null) {
          failureCode = error instanceof VoiceboxError ? error.code : "unavailable";
        }
        throw error;
      });
    }
    if (ended) return Promise.resolve(null);
    return new Promise<ArrayBuffer | null>((resolve, reject) => {
      const onAbort = () => {
        waiter = null;
        reject(abortError());
      };
      waiter = () => {
        signal.removeEventListener("abort", onAbort);
        resolve(next(signal));
      };
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  return { push, end, failure, next };
}

/**
 * `playAudio` from `feed`'s first chunk. Stale whenever `ticket` stops being current, including
 * mid-wait: a `controller.subscribe` listener aborts the wait as soon as that happens. `deadline`,
 * when given, bounds that same first-chunk wait only: an already-aborted `deadline` acts at once,
 * and one that fires mid-wait fails as `{ failed: "voicebox_timeout" }` — unless the ticket has
 * also gone stale, which still wins. Once the first chunk is in hand, `deadline`'s listener is
 * torn down (with the staleness one, in `finally`), so a later firing changes nothing.
 */
export async function startAudio(
  controller: Pick<ReadAloud, "isCurrent" | "playAudio" | "subscribe">,
  ticket: number,
  owner: string,
  feed: VoiceboxFeed,
  deadline?: AbortSignal,
): Promise<"playing" | "stale" | { failed: string }> {
  if (!controller.isCurrent(ticket)) return "stale";
  const abort = new AbortController();
  const unsubscribe = controller.subscribe(() => {
    if (!controller.isCurrent(ticket)) abort.abort();
  });
  const onDeadline = () => abort.abort();
  if (deadline?.aborted) abort.abort();
  else deadline?.addEventListener("abort", onDeadline);
  try {
    let first: ArrayBuffer | null;
    try {
      first = await feed.next(abort.signal);
    } catch {
      if (!controller.isCurrent(ticket)) return "stale";
      if (deadline?.aborted) return { failed: "voicebox_timeout" };
      return { failed: feed.failure() ?? "unavailable" };
    }
    if (!controller.isCurrent(ticket)) return "stale";
    if (first === null) return { failed: "nothing" };
    return controller.playAudio(ticket, owner, first, feed) ? "playing" : "stale";
  } finally {
    unsubscribe();
    deadline?.removeEventListener("abort", onDeadline);
  }
}
