import { boundedJson } from "../boundedResponse";
import {
  parseCallDetail,
  parseCalls,
  parseChangeActResult,
  parseDataDeleted,
  parseDataRecord,
  parseDataRecords,
  parseDataSummary,
  parseDeleted,
  parseError,
  parseLauncher,
  parseMission,
  parsePage,
  parsePageMissions,
  parseProposalResult,
  parseQuestionProposal,
  parseTestResult,
} from "./parse";
import { type DataQuery, type DecisionLoopApi, DecisionLoopApiError } from "./types";

/**
 * `createDecisionLoopApi` calls the routes under /api/v2/decision/, following this app's REST
 * client house rules (clients/app/shellApi.ts's header comment):
 * - `Authorization: Bearer <token>` only, relative paths, `cache: "no-store"`, never a query
 *   string; `content-type: application/json` only when the call has a body.
 * - One `AbortController` per call on a `setTimeout` a fake clock can own (never
 *   `AbortSignal.timeout`), merged with the caller's own signal where a route offers one; 15 s.
 * - Every response is read through `boundedJson` (256 KiB); an over-cap or unparsable body, or
 *   one a route's `parse.ts` checker refuses, is `invalid_response`.
 * - A refusal with a parsable `{error: {code, message}}` body keeps that code and the real
 *   status; otherwise the code comes from the status. Server text never reaches
 *   `Error.message`: `DecisionLoopApiError`'s message is the code alone.
 * - `saveKey`'s `key` argument rides straight into that one request body and is kept nowhere
 *   else: no field, no closure variable outlives the call, and nothing here logs it.
 */

const BASE = "/api/v2/decision";
const MAX_RESPONSE = 256 * 1024;
const TIMEOUT_MS = 15_000;
const ITEM_KEY = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** A refusal's code when its body is not a parsable `{error: {code, message}}` (the routes' own
 * codes, minus `limit`, which the server only ever sends in a parsable body). */
const STATUS_CODES: Readonly<Record<number, string>> = {
  401: "unauthorized",
  404: "unknown_item",
  409: "not_applicable",
  413: "too_large",
  415: "invalid_representation",
  429: "busy",
  503: "unavailable",
};

function statusCode(status: number): string {
  return STATUS_CODES[status] ?? "failed";
}

function refusalFrom(status: number, body: unknown): DecisionLoopApiError {
  const parsed = parseError(body);
  return new DecisionLoopApiError(parsed ? parsed.error.code : statusCode(status), status);
}

export function createDecisionLoopApi(
  token: string,
  fetchImpl: typeof fetch = fetch,
): DecisionLoopApi {
  async function transport(
    path: string,
    method: "GET" | "POST" | "DELETE",
    signal: AbortSignal | undefined,
    payload: unknown,
  ): Promise<{ status: number; ok: boolean; body: unknown }> {
    if (signal?.aborted) throw new DecisionLoopApiError("cancelled", 0);
    const controller = new AbortController();
    const relay = () => controller.abort();
    signal?.addEventListener("abort", relay, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, TIMEOUT_MS);
    try {
      let response: Response;
      try {
        response = await fetchImpl(path, {
          method,
          signal: controller.signal,
          cache: "no-store",
          headers: {
            authorization: `Bearer ${token}`,
            ...(payload === undefined ? {} : { "content-type": "application/json" }),
          },
          ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
        });
      } catch {
        if (signal?.aborted) throw new DecisionLoopApiError("cancelled", 0);
        if (timedOut) throw new DecisionLoopApiError("timeout", 0);
        throw new DecisionLoopApiError("unavailable", 0);
      }
      let body: unknown;
      try {
        body = await boundedJson(response, MAX_RESPONSE);
      } catch {
        body = undefined;
      }
      return { status: response.status, ok: response.ok, body };
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", relay);
    }
  }

  async function send<T>(
    path: string,
    method: "GET" | "POST" | "DELETE",
    signal: AbortSignal | undefined,
    payload: unknown,
    parse: (value: unknown) => T | null,
  ): Promise<T> {
    const { status, ok, body } = await transport(path, method, signal, payload);
    if (!ok) throw refusalFrom(status, body);
    const parsed = parse(body);
    if (parsed === null) throw new DecisionLoopApiError("invalid_response", status);
    return parsed;
  }

  async function sendVoid(
    path: string,
    method: "POST" | "DELETE",
    payload: unknown,
  ): Promise<void> {
    const { status, ok, body } = await transport(path, method, undefined, payload);
    if (!ok) throw refusalFrom(status, body);
  }

  return {
    async launcher(signal) {
      return send(`${BASE}/launcher`, "GET", signal, undefined, parseLauncher);
    },
    async openLauncher(signal) {
      return send(`${BASE}/launcher/open`, "POST", signal, undefined, parseLauncher);
    },
    async presence(body) {
      return sendVoid(`${BASE}/presence`, "POST", body);
    },
    async feedback(body) {
      return send(`${BASE}/feedback`, "POST", undefined, body, parseLauncher);
    },
    async observe(event, itemKeys) {
      return sendVoid(`${BASE}/observe`, "POST", { event, item_keys: itemKeys });
    },
    async report(topic, source) {
      return send(`${BASE}/reports`, "POST", undefined, { topic, source }, parsePage);
    },
    async page(signal) {
      return send(`${BASE}/page`, "GET", signal, undefined, parsePage);
    },
    async preference(body) {
      return send(`${BASE}/preferences`, "POST", undefined, body, parsePage);
    },
    async deleteEpisodes() {
      return send(`${BASE}/episodes`, "DELETE", undefined, undefined, parseDeleted);
    },
    async saveKey(key) {
      return send(`${BASE}/key`, "POST", undefined, { key }, parsePage);
    },
    async removeKey() {
      return send(`${BASE}/key`, "DELETE", undefined, undefined, parsePage);
    },
    async test() {
      return send(`${BASE}/test`, "POST", undefined, undefined, parseTestResult);
    },
    async calls(signal) {
      return send(`${BASE}/calls`, "GET", signal, undefined, parseCalls);
    },
    async call(callId, signal) {
      if (!UUID.test(callId)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/calls/${encodeURIComponent(callId)}`,
        "GET",
        signal,
        undefined,
        parseCallDetail,
      );
    },
    async missions(signal) {
      return send(`${BASE}/missions`, "GET", signal, undefined, parsePageMissions);
    },
    async startMission(itemKey, work) {
      return send(`${BASE}/missions`, "POST", undefined, { item_key: itemKey, work }, parseMission);
    },
    async stopMission(missionId) {
      return send(
        `${BASE}/missions/${encodeURIComponent(missionId)}/stop`,
        "POST",
        undefined,
        undefined,
        parseMission,
      );
    },
    async stopAllMissions() {
      return send(`${BASE}/missions/stop`, "POST", undefined, undefined, parsePageMissions);
    },
    async startSeat(routineId) {
      if (!UUID.test(routineId)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/seats/${encodeURIComponent(routineId)}/start`,
        "POST",
        undefined,
        undefined,
        parsePageMissions,
      );
    },
    async stopSeat(routineId) {
      if (!UUID.test(routineId)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/seats/${encodeURIComponent(routineId)}/stop`,
        "POST",
        undefined,
        undefined,
        parsePageMissions,
      );
    },
    async runChecks(workspaceId) {
      if (!UUID.test(workspaceId)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/checks/${encodeURIComponent(workspaceId)}/run`,
        "POST",
        undefined,
        undefined,
        parsePageMissions,
      );
    },
    async stopChecks(workspaceId) {
      if (!UUID.test(workspaceId)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/checks/${encodeURIComponent(workspaceId)}/stop`,
        "POST",
        undefined,
        undefined,
        parsePageMissions,
      );
    },
    async suggestInbox(itemKey) {
      if (!ITEM_KEY.test(itemKey)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/inbox/suggest`,
        "POST",
        undefined,
        { item_key: itemKey },
        parsePageMissions,
      );
    },
    async createProposal(name, question) {
      return send(`${BASE}/proposals`, "POST", undefined, { name, question }, parseProposalResult);
    },
    async proposal(id, signal) {
      if (!UUID.test(id)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/proposals/${encodeURIComponent(id)}`,
        "GET",
        signal,
        undefined,
        parseQuestionProposal,
      );
    },
    async actProposal(id, action, revision, reason) {
      if (!UUID.test(id)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/proposals/${encodeURIComponent(id)}`,
        "POST",
        undefined,
        { action, revision, reason: reason ?? null },
        parseProposalResult,
      );
    },
    async actChange(id, action, revision, reason) {
      if (!UUID.test(id)) throw new DecisionLoopApiError("invalid_input", 0);
      return send(
        `${BASE}/changes/${encodeURIComponent(id)}`,
        "POST",
        undefined,
        { action, revision, reason: reason ?? null },
        parseChangeActResult,
      );
    },
    async dataSummary(signal) {
      return send(`${BASE}/data`, "GET", signal, undefined, parseDataSummary);
    },
    async dataRecords(query, signal) {
      return send(`${BASE}/data/records`, "POST", signal, query, parseDataRecords);
    },
    async dataRecord(kind, id, signal) {
      if (typeof id !== "string" || !(kind === "decisions" ? UUID : ITEM_KEY).test(id)) {
        throw new DecisionLoopApiError("invalid_input", 0);
      }
      return send(`${BASE}/data/record`, "POST", signal, { kind, id }, parseDataRecord);
    },
    async deleteData(query: DataQuery) {
      if (
        query.ids !== null &&
        (!Array.isArray(query.ids) ||
          query.ids.length < 1 ||
          query.ids.length > 50 ||
          new Set(query.ids).size !== query.ids.length ||
          query.ids.some(
            (id) =>
              typeof id !== "string" || !(query.kind === "suggestions" ? ITEM_KEY : UUID).test(id),
          ))
      )
        throw new DecisionLoopApiError("invalid_input", 0);
      return send(`${BASE}/data/delete`, "POST", undefined, query, parseDataDeleted);
    },
  };
}
