/** Acceptance (frozen): the app store routes messages and keeps snapshot identity stable. */
import { expect, test, vi } from "vitest";
import type { Envelope, ServerMessage } from "../protocol";
import { AppStore } from "./store";

const AT = "2026-09-11T08:00:00.000Z";

function userMessage(sessionId: string, seq: number): Envelope {
  return {
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type: "user.message",
    session_id: sessionId,
    seq,
    data: { text: `hi ${sessionId}`, command: "", at: AT },
  };
}

test("envelopes go to their session; untouched sessions keep their identity", () => {
  const store = new AppStore();
  store.dispatch(userMessage("a", 1));
  store.dispatch(userMessage("b", 1));
  const before = store.getSnapshot();
  expect(Object.keys(before.sessions).sort()).toEqual(["a", "b"]);
  store.dispatch(userMessage("a", 2));
  const after = store.getSnapshot();
  expect(after).not.toBe(before);
  expect(after.sessions.b).toBe(before.sessions.b);
  expect(after.sessions.a?.turns).toHaveLength(2);
});

test("the snapshot is stable between changes and listeners hear each change", () => {
  const store = new AppStore();
  const listener = vi.fn();
  const stop = store.subscribe(listener);
  const first = store.getSnapshot();
  expect(store.getSnapshot()).toBe(first);
  expect(first).toMatchObject({ sessions: {}, others: [], status: "connecting" });
  store.dispatch(userMessage("a", 1));
  store.setStatus("open");
  expect(listener).toHaveBeenCalledTimes(2);
  expect(store.getSnapshot().status).toBe("open");
  stop();
  store.setStatus("reconnecting");
  expect(listener).toHaveBeenCalledTimes(2);
});

test("vault sessions, history pages and local updates", () => {
  const store = new AppStore();
  const other = { session_id: "x", entrypoint: "claude-vscode", pid: 42, title: "" };
  store.dispatch({
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type: "vault.sessions",
    session_id: "",
    seq: 0,
    data: { others: [other], live: [] },
  });
  expect(store.getSnapshot().others).toEqual([other]);
  const page: ServerMessage = {
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type: "history.page",
    session_id: "a",
    data: {
      events: [{ type: "user.message", data: { text: "old", command: "", at: AT } }],
      next_before_uuid: null,
    },
  };
  store.dispatch(page); // nobody asked for it
  expect(store.getSnapshot().sessions.a).toBeUndefined();
  store.update("a", (view) => ({
    ...view,
    generation: "11111111-1111-4111-8111-111111111111",
    requestId: "",
    historyRequestId: "",
    historyLoading: true,
  })); // a view from nothing
  store.dispatch(page);
  store.dispatch(page); // a duplicate: the first one ended the loading
  const view = store.getSnapshot().sessions.a;
  expect(view?.turns.map((t) => t.user?.text)).toEqual(["old"]);
  expect(view?.historyLoading).toBe(false);
  store.update("a", (v) => ({ ...v, mode: "plan" }));
  expect(store.getSnapshot().sessions.a?.mode).toBe("plan");
});

test("remove forgets one session's view and nothing else", () => {
  const store = new AppStore();
  store.dispatch(userMessage("a", 1));
  store.dispatch(userMessage("b", 1));
  const before = store.getSnapshot();
  const listener = vi.fn();
  store.subscribe(listener);
  store.remove("a");
  expect(Object.keys(store.getSnapshot().sessions)).toEqual(["b"]);
  expect(store.getSnapshot().sessions.b).toBe(before.sessions.b);
  expect(listener).toHaveBeenCalledTimes(1);
  const after = store.getSnapshot();
  store.remove("a");
  expect(store.getSnapshot()).toBe(after);
  expect(listener).toHaveBeenCalledTimes(1);
});
