/**
 * REST-fed state (the sessions list and the rails) as an external store for
 * useSyncExternalStore (src/accept/m3b/rest.accept.test.ts pins the contract).
 *
 * Rules:
 * - createRestStore(rest, appStore, options?) → RestStore: getSnapshot(), subscribe(listener)
 *   with AppStore's identity rules (a new snapshot object per change, the same object until
 *   the next), refreshSessions(), refreshRails(), start(), stop().
 * - The first snapshot is {sessions: [], rails: null, railsStatus: "loading", error: null,
 *   sessionsStatus: "loading", sessionsReason: "", sessionsOmitted: 0}.
 *   Starting a later rails read marks retained data loading; only the latest read can mark it
 *   ready or error. Session-list success never changes this independent observation state.
 * - The sessions list carries its own observation, never mixed with the rails one: a
 *   read marks it loading when it starts, `ready`/`partial` replace the rows, and a rejection
 *   (`transport`), a typed `state: "error"` or a body that is not a `SessionsResult`
 *   (`malformed`) keep the rows already on screen and mark them stale rather than emptying the
 *   rail. A sessions result never touches railsStatus, and a rails result never touches this.
 * - refreshSessions() / refreshRails() call rest.sessions() / rest.rails() and commit the
 *   result. While a call of one kind is in flight, further requests of that kind are folded
 *   into one run after it settles: never two concurrent, never more than one queued. The
 *   returned promise settles once the refresh it started or joined has been committed (it
 *   never rejects). A rejection keeps the previous data and sets `error` to the RestError's
 *   status (0 for any other failure); the next success of either kind clears it.
 * - start() refreshes both at once and again every `intervalMs` (default 60_000) until stop().
 *   It also subscribes to appStore: when the number of views whose last turn is running drops
 *   (a turn ended) both are refreshed; when `others` changes identity (a vault.sessions frame
 *   arrived) sessions are refreshed. stop() clears the interval and the subscription; start()
 *   while started is a no-op. Timers are the global ones (tests fake them).
 *
 * Implementation note: start()'s own kickoff, its interval tick and its appStore-triggered
 * refreshes call the fetchers directly (unconditionally) rather than through the coalescing
 * `refreshSessions`/`refreshRails` below — those two are folding gates for *concurrent explicit
 * callers* (the acceptance suite's "coalesce" test), which is a promise-settlement fact that
 * only becomes visible after a microtask turns over. start()'s own triggers are each a single,
 * independent reason to refresh and are expected to fire every time regardless of whatever an
 * earlier fetch is still doing (the acceptance suite's interval and appStore tests both assert
 * a fresh call count synchronously, with no `await` giving a settlement microtask a chance to
 * run in between).
 *
 * Ordering note: because every trigger really does fire its own request regardless of one
 * already in flight, two requests of the same kind can be outstanding at once, and a slow
 * first one can still be settling after a fast one started later already committed. Each
 * request is stamped with a generation number at the moment it starts; a response — success or
 * failure — only commits when its generation is still the newest one issued for that kind.
 * A slower, earlier-started response therefore never overwrites a fresher, later-started one
 * that already landed, whichever order they actually resolve in.
 */
import type { Rest } from "../api";
import { RestError } from "../api";
import type { Rails, SessionRow } from "../protocol";
import { observeSessions, type SessionsObservationReason } from "./catalog";
import type { AppSnapshot, AppStore } from "./store";

export interface RestSnapshot {
  sessions: SessionRow[];
  rails: Rails | null;
  /** Only a completed current rails read can supply a trusted Git observation. */
  railsStatus: "loading" | "ready" | "error";
  /** The last failure's HTTP status (0 when it was not an HTTP error), cleared on success. */
  error: number | null;
  /** What the latest listing read observed about `sessions`; independent of railsStatus. */
  sessionsStatus: "loading" | "ready" | "partial" | "error";
  sessionsReason: SessionsObservationReason;
  /** Transcripts the latest `partial` listing left out; 0 in every other state. */
  sessionsOmitted: number;
}

export interface RestStore {
  getSnapshot(): RestSnapshot;
  subscribe(listener: () => void): () => void;
  refreshSessions(): Promise<void>;
  refreshRails(): Promise<void>;
  start(): void;
  stop(): void;
}

export interface RestStoreOptions {
  intervalMs?: number;
}

function errorStatus(error: unknown): number {
  return error instanceof RestError ? error.status : 0;
}

function runningTurnCount(snapshot: AppSnapshot): number {
  let count = 0;
  for (const view of Object.values(snapshot.sessions)) {
    if (view.turns.at(-1)?.status === "running") count += 1;
  }
  return count;
}

/** Folds concurrent calls of one kind: never two requests in flight, never more than one
 * queued behind the current one; every caller's promise settles once its run has committed. */
function coalesce(run: () => Promise<void>): () => Promise<void> {
  let inFlight: Promise<void> | null = null;
  let queued: Promise<void> | null = null;

  function runTracked(): Promise<void> {
    const p = run();
    inFlight = p;
    void p.then(() => {
      if (inFlight === p) inFlight = null;
    });
    return p;
  }

  return function trigger(): Promise<void> {
    if (!inFlight) return runTracked();
    if (!queued) {
      const waitFor = inFlight;
      queued = waitFor.then(() => {
        queued = null;
        return runTracked();
      });
    }
    return queued;
  };
}

export function createRestStore(
  rest: Rest,
  appStore: AppStore,
  options: RestStoreOptions = {},
): RestStore {
  const intervalMs = options.intervalMs ?? 60_000;
  let snapshot: RestSnapshot = {
    sessions: [],
    rails: null,
    railsStatus: "loading",
    error: null,
    sessionsStatus: "loading",
    sessionsReason: "",
    sessionsOmitted: 0,
  };
  const listeners = new Set<() => void>();

  function commit(patch: Partial<RestSnapshot>): void {
    snapshot = { ...snapshot, ...patch };
    for (const listener of [...listeners]) listener();
  }

  // Generation counters: the request started most recently for each kind is the only one
  // allowed to commit, so a slower earlier one landing after it can't overwrite fresher data.
  let sessionsGen = 0;
  let railsGen = 0;

  async function runSessions(): Promise<void> {
    const gen = ++sessionsGen;
    if (snapshot.sessionsStatus !== "loading") commit({ sessionsStatus: "loading" });
    try {
      const observed = observeSessions(await rest.sessions());
      if (gen !== sessionsGen) return;
      // A read that reached the server clears `error` whatever it observed: `error` is
      // "the last request failed" (its HTTP status), not this listing's own typed state.
      if (observed.status === "error")
        // The rows on screen are the newest ones anyone read; emptying the rail would claim
        // the vault has no sessions, which a failed listing cannot know.
        commit({
          sessionsStatus: "error",
          sessionsReason: observed.reason,
          sessionsOmitted: 0,
          error: null,
        });
      else
        commit({
          sessions: observed.sessions,
          sessionsStatus: observed.status,
          sessionsReason: observed.reason,
          sessionsOmitted: observed.omitted,
          error: null,
        });
    } catch (error) {
      // Only the newest read may speak, as on the rails path; api.ts's 30 s abort bounds it, so
      // a trigger stream faster than that window is what keeps the rail "loading", not a hang.
      if (gen === sessionsGen)
        commit({
          sessionsStatus: "error",
          sessionsReason: "transport",
          sessionsOmitted: 0,
          error: errorStatus(error),
        });
    }
  }

  async function runRails(): Promise<void> {
    const gen = ++railsGen;
    if (snapshot.railsStatus !== "loading") commit({ railsStatus: "loading" });
    try {
      const rails = await rest.rails();
      if (gen === railsGen) commit({ rails, railsStatus: "ready", error: null });
    } catch (error) {
      if (gen === railsGen) commit({ railsStatus: "error", error: errorStatus(error) });
    }
  }

  const refreshSessions = coalesce(runSessions);
  const refreshRails = coalesce(runRails);

  function pollBoth(): void {
    void runSessions();
    void runRails();
  }

  let started = false;
  let intervalId: ReturnType<typeof setInterval> | undefined;
  let unsubscribeApp: (() => void) | undefined;

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    refreshSessions,
    refreshRails,
    start() {
      if (started) return;
      started = true;
      pollBoth();
      intervalId = setInterval(pollBoth, intervalMs);
      let lastRunning = runningTurnCount(appStore.getSnapshot());
      let lastOthers = appStore.getSnapshot().others;
      unsubscribeApp = appStore.subscribe(() => {
        const appSnapshot = appStore.getSnapshot();
        const running = runningTurnCount(appSnapshot);
        if (running < lastRunning) pollBoth();
        if (appSnapshot.others !== lastOthers) void runSessions();
        lastRunning = running;
        lastOthers = appSnapshot.others;
      });
    },
    stop() {
      if (!started) return;
      started = false;
      clearInterval(intervalId);
      intervalId = undefined;
      unsubscribeApp?.();
      unsubscribeApp = undefined;
    },
  };
}
