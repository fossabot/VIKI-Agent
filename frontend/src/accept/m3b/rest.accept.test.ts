/** Acceptance (frozen): the REST store's refresh, coalescing, error and trigger rules. */
import { afterEach, expect, test, vi } from "vitest";
import { type Rest, RestError } from "../../api";
import type { Envelope, Rails, SearchResult, SessionRow, SessionsResult } from "../../protocol";
import { createRestStore } from "../../state/rest";
import { AppStore } from "../../state/store";
import { emptyRails, sessionRow } from "./fakes";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Every call hands back a fresh deferred the test settles by hand. */
class ScriptedRest implements Rest {
  sessionsQ: Deferred<SessionRow[]>[] = [];
  railsQ: Deferred<Rails>[] = [];

  sessions(): Promise<SessionsResult> {
    const d = deferred<SessionRow[]>();
    this.sessionsQ.push(d);
    return d.promise.then((sessions) => ({ sessions, state: "ready", reason: "", omitted: 0 }));
  }

  rails(): Promise<Rails> {
    const d = deferred<Rails>();
    this.railsQ.push(d);
    return d.promise;
  }

  search(): Promise<SearchResult> {
    const result: SearchResult = {
      hits: [],
      state: "ready",
      reason: "",
      scanned: 0,
      total: 0,
      request_id: "",
    };
    return Promise.resolve(result);
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

afterEach(() => {
  vi.useRealTimers();
});

test("the first snapshot is empty; a refresh commits a new snapshot and notifies", async () => {
  const rest = new ScriptedRest();
  const store = createRestStore(rest, new AppStore());
  const before = store.getSnapshot();
  expect(before).toEqual({
    sessions: [],
    rails: null,
    railsStatus: "loading",
    error: null,
    sessionsStatus: "loading",
    sessionsReason: "",
    sessionsOmitted: 0,
  });
  expect(store.getSnapshot()).toBe(before);
  const listener = vi.fn();
  store.subscribe(listener);
  const done = store.refreshRails();
  const rails = { ...emptyRails(), counts: { knowledge: 1, sources: 0, inbox: 0 } };
  rest.railsQ[0]?.resolve(rails);
  await done;
  expect(store.getSnapshot()).not.toBe(before);
  expect(store.getSnapshot().rails).toEqual(rails);
  expect(listener).toHaveBeenCalledTimes(1);
  const rows = [sessionRow({ session_id: "a" })];
  const p = store.refreshSessions();
  rest.sessionsQ[0]?.resolve(rows);
  await p;
  expect(store.getSnapshot().sessions).toEqual(rows);
  expect(listener).toHaveBeenCalledTimes(2);
});

test("requests of one kind coalesce: never two in flight, at most one queued", async () => {
  const rest = new ScriptedRest();
  const store = createRestStore(rest, new AppStore());
  const first = store.refreshRails();
  const second = store.refreshRails();
  const third = store.refreshRails();
  expect(rest.railsQ).toHaveLength(1);
  rest.railsQ[0]?.resolve(emptyRails());
  await first;
  expect(rest.railsQ).toHaveLength(2); // the folded second run, once
  const rails = { ...emptyRails(), counts: { knowledge: 7, sources: 0, inbox: 0 } };
  rest.railsQ[1]?.resolve(rails);
  await Promise.all([second, third]);
  expect(rest.railsQ).toHaveLength(2);
  expect(store.getSnapshot().rails?.counts.knowledge).toBe(7);
});

test("a failure keeps the previous data and reports the status; the next success clears it", async () => {
  const rest = new ScriptedRest();
  const store = createRestStore(rest, new AppStore());
  const rows = [sessionRow({ session_id: "a" })];
  let p = store.refreshSessions();
  rest.sessionsQ[0]?.resolve(rows);
  await p;
  p = store.refreshSessions();
  rest.sessionsQ[1]?.reject(new RestError(500, "Internal Server Error"));
  await p;
  expect(store.getSnapshot().sessions).toEqual(rows);
  expect(store.getSnapshot().error).toBe(500);
  p = store.refreshRails();
  rest.railsQ[0]?.reject(new TypeError("Failed to fetch"));
  await p;
  expect(store.getSnapshot().error).toBe(0);
  p = store.refreshRails();
  rest.railsQ[1]?.resolve(emptyRails());
  await p;
  expect(store.getSnapshot().error).toBeNull();
  expect(store.getSnapshot().sessions).toEqual(rows);
});

test("start() fetches both at once and on every interval until stop(); start twice is one", () => {
  vi.useFakeTimers();
  const rest = new ScriptedRest();
  const store = createRestStore(rest, new AppStore(), { intervalMs: 1000 });
  store.start();
  store.start();
  expect(rest.sessionsQ).toHaveLength(1);
  expect(rest.railsQ).toHaveLength(1);
  rest.sessionsQ[0]?.resolve([]);
  rest.railsQ[0]?.resolve(emptyRails());
  vi.advanceTimersByTime(999);
  expect(rest.railsQ).toHaveLength(1);
  vi.advanceTimersByTime(1);
  expect(rest.sessionsQ).toHaveLength(2);
  expect(rest.railsQ).toHaveLength(2);
  store.stop();
  vi.advanceTimersByTime(5000);
  expect(rest.sessionsQ).toHaveLength(2);
  expect(rest.railsQ).toHaveLength(2);
});

test("a turn ending refreshes both; a vault.sessions frame refreshes the sessions", () => {
  vi.useFakeTimers();
  const rest = new ScriptedRest();
  const app = new AppStore();
  const store = createRestStore(rest, app, { intervalMs: 60_000 });
  store.start();
  rest.sessionsQ[0]?.resolve([]);
  rest.railsQ[0]?.resolve(emptyRails());
  app.dispatch(userMessage("a", 1)); // a turn starts: nothing to refresh yet
  expect(rest.sessionsQ).toHaveLength(1);
  expect(rest.railsQ).toHaveLength(1);
  app.dispatch(turnDone("a", 2));
  expect(rest.sessionsQ).toHaveLength(2);
  expect(rest.railsQ).toHaveLength(2);
  app.dispatch({
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type: "vault.sessions",
    session_id: "",
    seq: 0,
    data: { others: [], live: [] },
  });
  expect(rest.sessionsQ).toHaveLength(3);
  expect(rest.railsQ).toHaveLength(2);
  store.stop();
  app.dispatch(userMessage("a", 3));
  app.dispatch(turnDone("a", 4));
  expect(rest.sessionsQ).toHaveLength(3);
  expect(rest.railsQ).toHaveLength(2);
});
