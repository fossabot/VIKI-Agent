/**
 * The catalog observations and every sentence the rail and the palette derive from them. What
 * matters here is that an unknown count is never printed as a zero and that a body this build
 * does not understand degrades to "error", never to "there is nothing".
 */
import { expect, test } from "vitest";
import { sessionRow } from "../accept/m3b/fakes";
import type { SearchHit, SearchResult } from "../protocol";
import {
  listingNote,
  observeSearch,
  observeSessions,
  SEARCH_FAILED,
  type SessionsView,
  searchNote,
  sessionsFreshness,
} from "./catalog";

const rows = [sessionRow({ session_id: "a" }), sessionRow({ session_id: "b" })];

const hit: SearchHit = { session_id: "a", title: "t", role: "user", snippet: "s", at: "" };

function result(over: Partial<SearchResult> = {}): SearchResult {
  return { hits: [], state: "ready", reason: "", scanned: 3, total: 3, request_id: "", ...over };
}

function view(over: Partial<SessionsView> = {}): SessionsView {
  return {
    sessions: rows,
    sessionsStatus: "ready",
    sessionsReason: "",
    sessionsOmitted: 0,
    ...over,
  };
}

test("observeSessions passes a well-formed listing through, partial count included", () => {
  expect(observeSessions({ sessions: rows, state: "ready", reason: "", omitted: 0 })).toEqual({
    sessions: rows,
    status: "ready",
    reason: "",
    omitted: 0,
  });
  expect(
    observeSessions({ sessions: rows, state: "partial", reason: "time_budget", omitted: 12 }),
  ).toEqual({ sessions: rows, status: "partial", reason: "time_budget", omitted: 12 });
  expect(
    observeSessions({ sessions: [], state: "error", reason: "project_unreadable", omitted: 0 }),
  ).toEqual({ sessions: [], status: "error", reason: "project_unreadable", omitted: 0 });
  const botRows = rows.map((row) => ({ ...row, bot: true }));
  expect(
    observeSessions({ sessions: botRows, state: "ready", reason: "", omitted: 0 }).sessions,
  ).toEqual(botRows);
});

test("observeSessions rejects malformed bodies", () => {
  const good = { sessions: rows, state: "ready", reason: "", omitted: 0 };
  for (const body of [
    undefined,
    null,
    "sessions",
    [],
    {},
    { ...good, state: "done" }, // an enum value this build does not know
    { ...good, state: undefined },
    { ...good, reason: "disk_on_fire" },
    { ...good, omitted: -1 },
    { ...good, omitted: 1.5 },
    { ...good, omitted: "3" },
    { ...good, sessions: undefined },
    { ...good, sessions: [{ session_id: "a" }] }, // a row missing its declared fields
    { ...good, sessions: [{ ...rows[0], other: "no" }] },
    { ...good, sessions: [{ ...rows[0], bot: "yes" }] },
    // The count is what makes a partial listing describable: "incomplete list · 0 not read
    // yet" would claim the list is both incomplete and missing nothing.
    { ...good, state: "partial", reason: "time_budget", omitted: 0 },
    { ...good, omitted: 4 }, // ready, yet claiming omissions
  ]) {
    const observed = observeSessions(body);
    expect(observed.status).toBe("error");
    expect(observed.reason).toBe("malformed");
    expect(observed.omitted).toBe(0);
  }
});

test("sessionsFreshness tells a first read from a refresh, and stale rows from none at all", () => {
  expect(sessionsFreshness(view({ sessionsStatus: "loading", sessions: [] }))).toBe("loading");
  expect(sessionsFreshness(view({ sessionsStatus: "loading" }))).toBe("refreshing");
  expect(sessionsFreshness(view())).toBe("ready");
  expect(sessionsFreshness(view({ sessionsStatus: "partial", sessionsOmitted: 1 }))).toBe(
    "partial",
  );
  expect(sessionsFreshness(view({ sessionsStatus: "error" }))).toBe("stale");
  expect(sessionsFreshness(view({ sessionsStatus: "error", sessions: [] }))).toBe("unavailable");
});

test("listingNote says what was read and never turns a failed read into an empty vault", () => {
  expect(listingNote(view({ sessionsStatus: "loading", sessions: [] }))).toBe("loading sessions…");
  // A refresh over rows already on screen is silent: the rows are still true.
  expect(listingNote(view({ sessionsStatus: "loading" }))).toBeNull();
  expect(listingNote(view())).toBeNull();
  expect(listingNote(view({ sessions: [] }))).toBe("no sessions yet");
  expect(
    listingNote(
      view({ sessionsStatus: "partial", sessionsReason: "time_budget", sessionsOmitted: 12 }),
    ),
  ).toBe("incomplete list · 12 not read yet");
  expect(
    listingNote(
      view({ sessionsStatus: "partial", sessionsReason: "read_failed", sessionsOmitted: 2 }),
    ),
  ).toBe("incomplete list · 2 unreadable");
  expect(listingNote(view({ sessionsStatus: "error", sessionsReason: "transport" }))).toBe(
    "sessions may be out of date · last read failed",
  );
  expect(
    listingNote(view({ sessionsStatus: "error", sessionsReason: "malformed", sessions: [] })),
  ).toBe("sessions unavailable");
});

test("observeSearch passes a well-formed result through and answers a bad body with an error", () => {
  const good = result({ hits: [hit], state: "partial", reason: "time_budget", scanned: 17 });
  expect(observeSearch(good)).toEqual(good);
  const failed = { hits: [], state: "error", reason: "", scanned: 0, total: 0, request_id: "" };
  for (const body of [
    undefined,
    null,
    [hit],
    {},
    { ...good, state: "maybe" },
    { ...good, reason: "gremlins" },
    { ...good, scanned: -2 },
    { ...good, total: null },
    { ...good, request_id: 7 },
    { ...good, hits: { 0: hit } },
    { ...good, hits: [{ ...hit, role: "system" }] },
    { ...good, hits: [{ ...hit, snippet: undefined }] },
  ]) {
    expect(observeSearch(body)).toEqual(failed);
  }
});

test("searchNote never lets a budget read as 'no matches'", () => {
  expect(searchNote(result({ hits: [hit] }), "milk")).toBeNull();
  expect(
    searchNote(result({ hits: [hit], state: "partial", scanned: 17, total: 41 }), "milk"),
  ).toBe("partial results · 17 of 41 sessions searched");
  expect(searchNote(result({ state: "partial", scanned: 17, total: 41 }), "milk")).toBe(
    "no matches in 17 of 41 sessions · search incomplete",
  );
  // A listing cut short before it listed anything leaves no denominator: "0 of 0 sessions"
  // would print a count nobody knows, which is exactly what the ADR forbids.
  expect(
    searchNote(result({ state: "partial", reason: "listing_partial", scanned: 0, total: 0 }), "x"),
  ).toBe("search incomplete · the session list could not be read");
  // One session listed and searched is a real denominator, and keeps the counted sentence.
  expect(
    searchNote(result({ state: "partial", reason: "listing_partial", scanned: 1, total: 1 }), "x"),
  ).toBe("no matches in 1 of 1 sessions · search incomplete");
  expect(searchNote(result({ state: "error", reason: "read_failed" }), "milk")).toBe(SEARCH_FAILED);
  // A superseded query never scanned; the palette drops it, and there is nothing to say.
  expect(searchNote(result({ state: "superseded" }), "milk")).toBeNull();
});

test("searchNote mirrors catalog.py's floor, so a refused query never reads as 'no matches'", () => {
  expect(searchNote(result(), "milk")).toBe("no matches");
  expect(searchNote(result(), "牛乳")).toBe("no matches");
  expect(searchNote(result(), "雨")).toBe("no matches"); // one kanji is a real search
  expect(searchNote(result(), "a")).toBe("type 2+ characters");
  expect(searchNote(result(), "ｘ")).toBe("type 2+ characters"); // NFKC-normalized to "x"
  expect(searchNote(result(), " a ")).toBe("type 2+ characters");
});
