/**
 * Typed catalog observations, and every word the rail and the palette say about them.
 *
 * Why here: `/api/sessions` and `/api/search` now answer with an observation, not a bare list —
 * a listing can be `partial` because a budget stopped it, a search can be `partial`,
 * `superseded` or a typed `error`. Believing such a body blindly is how a failed listing used
 * to read as "no sessions" and a search that scanned 17 of 41 transcripts read as "no matches".
 *
 * Rules:
 * - observeSessions / observeSearch validate a body the way state/git.ts validates a Git chip:
 *   every enum value must be one this build knows and every field must have its declared type.
 *   A body that fails is `malformed`, not a zero — failing closed keeps a browser bundle that
 *   is out of step with the server from inventing counts the server never sent.
 * - sessionsFreshness turns the store's status plus the rows it is holding into what the rail
 *   may honestly claim; listingNote and searchNote are the only place any of this copy lives,
 *   so rewording the notes is an edit to this file alone.
 * - Counts are printed only when they are known: `incomplete list · N …` is reachable only for
 *   a body whose `omitted` is a positive integer, and a partial body without one is malformed.
 */
import type {
  SearchHit,
  SearchReason,
  SearchResult,
  SearchState,
  SessionRow,
  SessionsReason,
  SessionsResult,
  SessionsState,
} from "../protocol";
import type { RestSnapshot } from "./rest";

/** The wire reasons plus the two the browser itself can observe: a request that never arrived
 * (`transport`) and a body that is not a `SessionsResult` (`malformed`). */
export type SessionsObservationReason = SessionsReason | "transport" | "malformed";

export interface SessionsObservation {
  sessions: SessionRow[];
  status: SessionsState;
  reason: SessionsObservationReason;
  omitted: number;
}

/** What the notes read: the rows the store is holding and its last observation of them. */
export type SessionsView = Pick<
  RestSnapshot,
  "sessions" | "sessionsStatus" | "sessionsReason" | "sessionsOmitted"
>;

export type SessionsFreshness =
  | "loading"
  | "refreshing"
  | "ready"
  | "partial"
  | "stale"
  | "unavailable";

const SESSIONS_STATES: readonly string[] = ["ready", "partial", "error"];
const SESSIONS_REASONS: readonly string[] = [
  "",
  "time_budget",
  "read_failed",
  "project_unreadable",
];
const SEARCH_STATES: readonly string[] = ["ready", "partial", "superseded", "error"];
const SEARCH_REASONS: readonly string[] = [
  "",
  "time_budget",
  "scan_budget",
  "file_budget",
  "response_budget",
  "listing_partial",
  "cancelled",
  "read_failed",
  "project_unreadable",
];
const SEARCH_ROLES: readonly string[] = ["title", "user", "assistant"];

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isSessionRow(value: unknown): boolean {
  const row = value as Partial<SessionRow> | null;
  return (
    typeof row === "object" &&
    row !== null &&
    typeof row.session_id === "string" &&
    typeof row.title === "string" &&
    typeof row.first_prompt === "string" &&
    typeof row.entrypoint === "string" &&
    typeof row.modified === "string" &&
    typeof row.cli_version === "string" &&
    typeof row.other === "boolean" &&
    (row.bot === undefined || typeof row.bot === "boolean")
  );
}

function isSearchHit(value: unknown): boolean {
  const hit = value as Partial<SearchHit> | null;
  return (
    typeof hit === "object" &&
    hit !== null &&
    typeof hit.session_id === "string" &&
    typeof hit.title === "string" &&
    typeof hit.snippet === "string" &&
    typeof hit.at === "string" &&
    SEARCH_ROLES.includes(hit.role as string)
  );
}

function malformedSessions(): SessionsObservation {
  return { sessions: [], status: "error", reason: "malformed", omitted: 0 };
}

/** A `SessionsResult` the rail may believe, or an `error`/`malformed` observation. */
export function observeSessions(body: unknown): SessionsObservation {
  const result = body as Partial<SessionsResult> | null;
  if (typeof result !== "object" || result === null) return malformedSessions();
  if (!SESSIONS_STATES.includes(result.state as string)) return malformedSessions();
  if (!SESSIONS_REASONS.includes(result.reason as string)) return malformedSessions();
  if (!isCount(result.omitted)) return malformedSessions();
  if (!Array.isArray(result.sessions) || !result.sessions.every(isSessionRow))
    return malformedSessions();
  // A listing is partial exactly when it left rows out. A partial body carrying no
  // count would make the note print "incomplete list · 0 not read yet" — a list that is both
  // incomplete and missing nothing. There is no honest sentence for that, so it is malformed.
  if ((result.state === "partial") !== result.omitted > 0) return malformedSessions();
  return {
    sessions: result.sessions,
    status: result.state as SessionsState,
    reason: result.reason as SessionsReason,
    omitted: result.omitted,
  };
}

/** A `SearchResult` the palette may paint, or a typed `error` standing in for a bad body. */
export function observeSearch(body: unknown): SearchResult {
  const result = body as Partial<SearchResult> | null;
  const failed: SearchResult = {
    hits: [],
    state: "error",
    reason: "",
    scanned: 0,
    total: 0,
    request_id: "",
  };
  if (typeof result !== "object" || result === null) return failed;
  if (!SEARCH_STATES.includes(result.state as string)) return failed;
  if (!SEARCH_REASONS.includes(result.reason as string)) return failed;
  if (!isCount(result.scanned) || !isCount(result.total)) return failed;
  if (typeof result.request_id !== "string") return failed;
  if (!Array.isArray(result.hits) || !result.hits.every(isSearchHit)) return failed;
  return {
    hits: result.hits,
    state: result.state as SearchState,
    reason: result.reason as SearchReason,
    scanned: result.scanned,
    total: result.total,
    request_id: result.request_id,
  };
}

/** What the rail may claim about the rows on screen. A read that failed leaves the rows it
 * already had, so "stale" and "unavailable" are different sentences, not one. */
export function sessionsFreshness(rest: SessionsView): SessionsFreshness {
  const hasRows = rest.sessions.length > 0;
  if (rest.sessionsStatus === "loading") return hasRows ? "refreshing" : "loading";
  if (rest.sessionsStatus === "error") return hasRows ? "stale" : "unavailable";
  return rest.sessionsStatus;
}

/** The rail's one note line, or null when the list speaks for itself. */
export function listingNote(rest: SessionsView): string | null {
  switch (sessionsFreshness(rest)) {
    case "loading":
      return "loading sessions…";
    case "refreshing":
      return null;
    case "ready":
      // A known zero: the project folder was listed and holds no transcript.
      return rest.sessions.length === 0 ? "no sessions yet" : null;
    case "partial":
      return rest.sessionsReason === "read_failed"
        ? `incomplete list · ${rest.sessionsOmitted} unreadable`
        : `incomplete list · ${rest.sessionsOmitted} not read yet`;
    case "stale":
      return "sessions may be out of date · last read failed";
    case "unavailable":
      return "sessions unavailable";
  }
}

/** A request is in flight; the palette says so rather than showing the last query's answer. */
export const SEARCHING = "searching…";
/** A request that never came back: there is no result to describe. */
export const SEARCH_FAILED = "search failed";

/**
 * Python's `str.isascii()`, which catalog.py branches on. Iterating code points rather than
 * UTF-16 units keeps the two sides identical for an astral character: its first unit is a high
 * surrogate, so either way it reads as non-ASCII, exactly as Python reads it.
 */
function isAscii(text: string): boolean {
  for (const ch of text) {
    if (ch.charCodeAt(0) > 0x7f) return false;
  }
  return true;
}

/**
 * catalog.py's short-query floor, mirrored character for character (normalize, then strip, then
 * the ASCII-shaped floor): two characters guards against one-letter Latin noise like "x", and
 * that reasoning does not apply to a single kanji, which is already a selective query. The
 * server answers a refused query with the same empty `hits` as a real zero-result search, so
 * only this mirror can tell the user which of the two they are looking at.
 */
function belowSearchFloor(query: string): boolean {
  const normalized = query.normalize("NFKC").trim();
  return normalized.length < (isAscii(normalized) ? 2 : 1);
}

/** The palette's note for a result it is about to paint, or null when the hits say it all.
 * A `superseded` result is dropped by the palette before this is reached. */
export function searchNote(result: SearchResult, query: string): string | null {
  if (result.state === "error") return SEARCH_FAILED;
  if (result.state === "superseded") return null;
  if (result.state === "partial") {
    // `total` counts the sessions the listing managed to list, so "0 of 0 sessions" would print
    // a denominator nobody knows. A partial search with none of them can only have been stopped
    // by that listing: every other reason needs at least one session scanned.
    if (result.total === 0) return "search incomplete · the session list could not be read";
    return result.hits.length > 0
      ? `partial results · ${result.scanned} of ${result.total} sessions searched`
      : `no matches in ${result.scanned} of ${result.total} sessions · search incomplete`;
  }
  if (result.hits.length > 0) return null;
  return belowSearchFloor(query) ? "type 2+ characters" : "no matches";
}
