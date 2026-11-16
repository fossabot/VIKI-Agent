/**
 * REST calls to the Python server (src/accept/m3b/api.accept.test.ts pins the contract).
 *
 * Rules:
 * - createRest(token, fetchImpl = fetch) → {sessions, search, rails}. Every call is a GET with
 *   `Authorization: Bearer <token>` and nothing else identifying: the token never travels in
 *   a URL. Paths are relative ("/api/..."), so the page's own origin serves them.
 * - sessions() → GET /api/sessions. search(q, requestId?) → GET /api/search?q=<q,
 *   percent-encoded, passed whole>[&request_id=<id>]. rails() → GET /api/rails. Each returns
 *   the response body as it stands: the sessions list and a search are observations, not bare
 *   lists, and unwrapping them here would throw the observation away.
 * - Every GET aborts after 30 s. A request that never settles would leave its surface
 *   "loading" for the rest of the session, the one state no note can recover from; an abort
 *   turns it into an ordinary rejection the store already knows how to report.
 * - A non-2xx response rejects with RestError {status, body: the response text}; a failed
 *   fetch, and the abort above, reject with that error unchanged. Nothing is retried or cached
 *   here: rest.ts decides when to ask again.
 */
import type { Rails, SearchResult, SessionsResult } from "./protocol";

const GET_TIMEOUT_MS = 30_000;

export class RestError extends Error {
  readonly status: number;
  readonly body: string;

  constructor(status: number, body: string) {
    super(`REST ${status}`);
    this.name = "RestError";
    this.status = status;
    this.body = body;
  }
}

export interface Rest {
  sessions(): Promise<SessionsResult>;
  search(q: string, requestId?: string): Promise<SearchResult>;
  rails(): Promise<Rails>;
}

export function createRest(token: string, fetchImpl: typeof fetch = fetch): Rest {
  async function get<T>(path: string): Promise<T> {
    // AbortController + setTimeout rather than AbortSignal.timeout: the latter exists under
    // vitest/jsdom but runs on a timer the fake clock does not own (measured: an advance of
    // 60 s never fires it), so the only way to test a 30 s abort would be to wait 30 real
    // seconds. This one fires on the clock the tests already control.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), GET_TIMEOUT_MS);
    try {
      const response = await fetchImpl(path, {
        method: "GET",
        headers: { authorization: `Bearer ${token}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new RestError(response.status, await response.text());
      return (await response.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }
  return {
    sessions: () => get<SessionsResult>("/api/sessions"),
    search: (q, requestId) => {
      const id = requestId ? `&request_id=${encodeURIComponent(requestId)}` : "";
      return get<SearchResult>(`/api/search?q=${encodeURIComponent(q)}${id}`);
    },
    rails: () => get<Rails>("/api/rails"),
  };
}
