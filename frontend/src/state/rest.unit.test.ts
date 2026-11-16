/**
 * Unit tests pinning createRestStore behaviour that rest.accept.test.ts leaves implicit: the
 * start()/stop() lifecycle (a start → stop → start sequence, what React's StrictMode does to
 * every effect on mount, must never leave two intervals ticking), generation ordering, and the
 * sessions observation that sits beside the rails one.
 */
import { afterEach, expect, test, vi } from "vitest";
import { emptyRails, sessionRow } from "../accept/m3b/fakes";
import { type Rest, RestError } from "../api";
import type { Envelope, Rails, SearchResult, SessionRow, SessionsResult } from "../protocol";
import { createRestStore } from "./rest";
import { AppStore } from "./store";

/** A listing the store may believe, so a test says only what it is actually about. */
function listing(sessions: SessionRow[], over: Partial<SessionsResult> = {}): SessionsResult {
  return { sessions, state: "ready", reason: "", omitted: 0, ...over };
}

function noHits(): SearchResult {
  return { hits: [], state: "ready", reason: "", scanned: 0, total: 0, request_id: "" };
}

/** Resolves every call at once (the exact request count is what these tests check). */
class ImmediateRest implements Rest {
  sessionCalls = 0;
  railCalls = 0;
  sessions(): Promise<SessionsResult> {
    this.sessionCalls += 1;
    return Promise.resolve(listing([]));
  }
  rails(): Promise<Rails> {
    this.railCalls += 1;
    return Promise.resolve(emptyRails());
  }
  search(): Promise<SearchResult> {
    return Promise.resolve(noHits());
  }
}

afterEach(() => {
  vi.useRealTimers();
});

interface Deferred<T> {
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
  promise: Promise<T>;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { resolve, reject, promise };
}

/** Every call hands back its own deferred, settled by hand and in any order (mirrors the m3b
 * acceptance suite's ScriptedRest, kept local here rather than exported from a frozen file). */
class ScriptedRest implements Rest {
  sessionsQ: Deferred<SessionsResult>[] = [];
  railsQ: Deferred<Rails>[] = [];
  sessions(): Promise<SessionsResult> {
    const d = deferred<SessionsResult>();
    this.sessionsQ.push(d);
    return d.promise;
  }
  rails(): Promise<Rails> {
    const d = deferred<Rails>();
    this.railsQ.push(d);
    return d.promise;
  }
  search(): Promise<SearchResult> {
    return Promise.resolve(noHits());
  }
}

const AT = "2026-09-12T08:00:00.000Z";

function userMessage(sessionId: string, seq: number): Envelope {
  return {
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type: "user.message",
    session_id: sessionId,
    seq,
    data: { text: "hi", command: "", at: AT },
  };
}

function turnDone(sessionId: string, seq: number): Envelope {
  return {
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type: "turn.done",
    session_id: sessionId,
    seq,
    data: {
      status: "success",
      usage: { input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_creation_tokens: 0 },
      context_percent: 10,
      duration_ms: 5,
    },
  };
}

test("a slow first-started refresh does not overwrite a fresher, later-started one", async () => {
  const rest = new ScriptedRest();
  const app = new AppStore();
  const store = createRestStore(rest, app, { intervalMs: 60_000 });

  store.start(); // kickoff A: sessionsQ[0] / railsQ[0], both slow
  app.dispatch(userMessage("a", 1));
  app.dispatch(turnDone("a", 2)); // turn ends -> pollBoth -> B: sessionsQ[1] / railsQ[1], fast
  app.dispatch({
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type: "vault.sessions",
    session_id: "",
    seq: 0,
    data: { others: [{ session_id: "z", entrypoint: "cli", pid: 1, title: "" }], live: [] },
  }); // others changed identity -> C: sessionsQ[2] only
  expect(rest.sessionsQ).toHaveLength(3);
  expect(rest.railsQ).toHaveLength(2);

  const fresh = [sessionRow({ session_id: "new" })];
  const freshRails: Rails = {
    ...emptyRails(),
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "fresh",
      committed_at: "",
      dirty: 7,
    },
  };
  // B and C (started after A) land first, with the fresh, post-turn state.
  rest.sessionsQ[1]?.resolve(listing(fresh));
  rest.sessionsQ[2]?.resolve(listing(fresh));
  rest.railsQ[1]?.resolve(freshRails);
  await Promise.resolve();
  await Promise.resolve();
  expect(store.getSnapshot().sessions).toEqual(fresh);
  expect(store.getSnapshot().sessionsStatus).toBe("ready");
  expect(store.getSnapshot().rails).toEqual(freshRails);

  // A (started first, cold) finally lands with the stale pre-turn state — and with a partial
  // observation, which must not re-mark the fresher listing as incomplete either.
  rest.sessionsQ[0]?.resolve(
    listing([sessionRow({ session_id: "old" })], {
      state: "partial",
      reason: "time_budget",
      omitted: 9,
    }),
  );
  rest.railsQ[0]?.resolve(emptyRails()); // the stale chip: clean tree
  await Promise.resolve();
  await Promise.resolve();

  // The stale, earlier-started response must not have won.
  expect(store.getSnapshot().sessions).toEqual(fresh);
  expect(store.getSnapshot().sessionsStatus).toBe("ready");
  expect(store.getSnapshot().sessionsOmitted).toBe(0);
  expect(store.getSnapshot().rails).toEqual(freshRails);
});

test("control case: a stale response landing ALONE (nothing fresher ever committed) still applies", async () => {
  const rest = new ScriptedRest();
  const store = createRestStore(rest, new AppStore(), { intervalMs: 60_000 });
  const p = store.refreshSessions();
  const rows = [sessionRow({ session_id: "a" })];
  rest.sessionsQ[0]?.resolve(listing(rows));
  await p;
  expect(store.getSnapshot().sessions).toEqual(rows);
});

test("start -> stop -> start leaves exactly one live interval, not two", () => {
  vi.useFakeTimers();
  const rest = new ImmediateRest();
  const store = createRestStore(rest, new AppStore(), { intervalMs: 1000 });
  store.start(); // one kickoff
  store.stop(); // must clear that interval before it ever fires
  store.start(); // a second kickoff, and a fresh interval
  expect(rest.sessionCalls).toBe(2);
  expect(rest.railCalls).toBe(2);
  vi.advanceTimersByTime(1000);
  // If stop() had failed to clear the first interval, two intervals would now both be due
  // and this would read 4, not 3.
  expect(rest.sessionCalls).toBe(3);
  expect(rest.railCalls).toBe(3);
  store.stop();
  vi.advanceTimersByTime(5000);
  expect(rest.sessionCalls).toBe(3);
  expect(rest.railCalls).toBe(3);
});

test("stop() before ever starting, or twice in a row, is a harmless no-op", () => {
  vi.useFakeTimers();
  const rest = new ImmediateRest();
  const store = createRestStore(rest, new AppStore(), { intervalMs: 1000 });
  expect(() => store.stop()).not.toThrow();
  store.start();
  store.stop();
  expect(() => store.stop()).not.toThrow();
  vi.advanceTimersByTime(10_000);
  expect(rest.sessionCalls).toBe(1); // only the one kickoff; stop() truly stopped the ticking
});

test("a rails refresh withdraws clean evidence and sessions success cannot revive a failed read", async () => {
  const rest = new ScriptedRest();
  const store = createRestStore(rest, new AppStore());
  const clean: Rails = {
    ...emptyRails(),
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "abc1234",
      committed_at: "",
      dirty: 0,
    },
  };
  let pending = store.refreshRails();
  rest.railsQ[0]?.resolve(clean);
  await pending;
  expect(store.getSnapshot().railsStatus).toBe("ready");
  pending = store.refreshRails();
  expect(store.getSnapshot().railsStatus).toBe("loading");
  expect(store.getSnapshot().rails).toBe(clean);
  rest.railsQ[1]?.reject(new Error("synthetic read failure"));
  await pending;
  const sessions = store.refreshSessions();
  rest.sessionsQ[0]?.resolve(listing([]));
  await sessions;
  expect(store.getSnapshot().error).toBeNull();
  expect(store.getSnapshot().railsStatus).toBe("error");
  expect(store.getSnapshot().rails).toBe(clean);
  pending = store.refreshRails();
  rest.railsQ[2]?.resolve(clean);
  await pending;
  expect(store.getSnapshot().railsStatus).toBe("ready");
});

test("a failed sessions read keeps the rows and marks them stale without touching rails", async () => {
  const rest = new ScriptedRest();
  const store = createRestStore(rest, new AppStore());
  const rows = [sessionRow({ session_id: "a" }), sessionRow({ session_id: "b" })];

  let pending = store.refreshSessions();
  rest.sessionsQ[0]?.resolve(listing(rows));
  await pending;
  const railsPending = store.refreshRails();
  rest.railsQ[0]?.resolve(emptyRails());
  await railsPending;
  expect(store.getSnapshot().sessions).toEqual(rows);
  expect(store.getSnapshot().sessionsStatus).toBe("ready");
  expect(store.getSnapshot().railsStatus).toBe("ready");

  // A typed error: the listing reached the server, which could not read the project folder.
  // The rows already on screen are the newest anyone has; emptying the rail would claim the
  // vault has no sessions, which this answer cannot know.
  pending = store.refreshSessions();
  expect(store.getSnapshot().sessionsStatus).toBe("loading"); // a refresh over retained rows
  rest.sessionsQ[1]?.resolve({
    sessions: [],
    state: "error",
    reason: "project_unreadable",
    omitted: 0,
  });
  await pending;
  expect(store.getSnapshot().sessions).toEqual(rows);
  expect(store.getSnapshot().sessionsStatus).toBe("error");
  expect(store.getSnapshot().sessionsReason).toBe("project_unreadable");
  // The request itself succeeded, so the transport-level `error` stays clear...
  expect(store.getSnapshot().error).toBeNull();
  // ...and a listing observation never speaks for the rails one.
  expect(store.getSnapshot().railsStatus).toBe("ready");

  // A rejection is a different failure: no body at all, and an HTTP status worth reporting.
  pending = store.refreshSessions();
  rest.sessionsQ[2]?.reject(new RestError(503, "Service Unavailable"));
  await pending;
  expect(store.getSnapshot().sessions).toEqual(rows);
  expect(store.getSnapshot().sessionsStatus).toBe("error");
  expect(store.getSnapshot().sessionsReason).toBe("transport");
  expect(store.getSnapshot().error).toBe(503);

  // A partial listing replaces the rows it did read, and carries what it left out.
  const first = rows[0];
  if (!first) throw new Error("row missing");
  pending = store.refreshSessions();
  rest.sessionsQ[3]?.resolve(
    listing([first], { state: "partial", reason: "time_budget", omitted: 4 }),
  );
  await pending;
  expect(store.getSnapshot().sessions).toEqual([first]);
  expect(store.getSnapshot().sessionsStatus).toBe("partial");
  expect(store.getSnapshot().sessionsOmitted).toBe(4);
  expect(store.getSnapshot().error).toBeNull();

  // And a failing rails read leaves that partial observation exactly as it was.
  const railsFail = store.refreshRails();
  rest.railsQ[1]?.reject(new RestError(500, "boom"));
  await railsFail;
  expect(store.getSnapshot().railsStatus).toBe("error");
  expect(store.getSnapshot().sessionsStatus).toBe("partial");
  expect(store.getSnapshot().sessionsOmitted).toBe(4);
});

test("a body that is not a SessionsResult is malformed, not an empty vault", async () => {
  const rest = new ScriptedRest();
  const store = createRestStore(rest, new AppStore());
  const rows = [sessionRow({ session_id: "a" })];
  let pending = store.refreshSessions();
  rest.sessionsQ[0]?.resolve(listing(rows));
  await pending;

  pending = store.refreshSessions();
  // What an older server (or a proxy) would answer: the bare list this route used to return.
  rest.sessionsQ[1]?.resolve(rows as unknown as SessionsResult);
  await pending;
  expect(store.getSnapshot().sessions).toEqual(rows);
  expect(store.getSnapshot().sessionsStatus).toBe("error");
  expect(store.getSnapshot().sessionsReason).toBe("malformed");
  expect(store.getSnapshot().sessionsOmitted).toBe(0);
});
