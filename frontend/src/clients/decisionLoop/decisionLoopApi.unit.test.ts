import { afterEach, describe, expect, it, vi } from "vitest";
import { createDecisionLoopApi } from "./decisionLoopApi";
import { decisionLoopExamples } from "./fake";
import {
  type DataQuery,
  DecisionLoopApiError,
  type FeedbackBody,
  type PreferenceBody,
  type PresenceBody,
} from "./types";

const EXAMPLES = decisionLoopExamples();
const CALL_ID = EXAMPLES.call_detail.call_id;
const MISSION_ID = EXAMPLES.mission.mission_id;
const ITEM_KEY = EXAMPLES.launcher.items[0]?.item_key ?? "";
const PROPOSAL = EXAMPLES.question_proposal.proposal;
const CHANGE = EXAMPLES.change_act.change;

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const noBody = (status: number) => new Response(null, { status });

function setup(response: Response) {
  const fetchImpl = vi.fn(async () => response);
  return { fetchImpl, api: createDecisionLoopApi("tok", fetchImpl as unknown as typeof fetch) };
}
function sent(fetchImpl: ReturnType<typeof vi.fn>, call = 0) {
  const [route, init] = fetchImpl.mock.calls[call] as unknown as [string, RequestInit];
  return { route, init, headers: init.headers as Record<string, string> };
}
const asError = async (promise: Promise<unknown>) =>
  (await promise.catch((error) => error)) as DecisionLoopApiError;
/** A fetch that never settles until its own request signal aborts, for timeout/cancel tests. */
const hangingFetch = (() =>
  vi.fn(
    (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  )) as unknown as () => typeof fetch;

afterEach(() => vi.useRealTimers());

describe("Decision API routes: method, path, body and parser", () => {
  it("reads data summary and records, gets record details, and deletes with the data routes", async () => {
    const summary = setup(reply(EXAMPLES.data_summary));
    expect(await summary.api.dataSummary()).toEqual(EXAMPLES.data_summary);
    expect(sent(summary.fetchImpl)).toMatchObject({
      route: "/api/v2/decision/data",
      init: { method: "GET" },
    });

    const query: DataQuery = {
      kind: "decisions",
      ids: null,
      topic: null,
      shown: null,
      answer: null,
      state: null,
      outcome: null,
      since: null,
      until: null,
      before: null,
    };
    const records = setup(reply(EXAMPLES.data_decisions));
    expect(await records.api.dataRecords(query)).toEqual(EXAMPLES.data_decisions);
    expect(sent(records.fetchImpl)).toMatchObject({
      route: "/api/v2/decision/data/records",
      init: { method: "POST" },
    });
    expect(JSON.parse(sent(records.fetchImpl).init.body as string)).toEqual(query);

    const detail = setup(reply(EXAMPLES.data_decision));
    expect(await detail.api.dataRecord("decisions", EXAMPLES.data_decision.id)).toEqual(
      EXAMPLES.data_decision,
    );
    expect(sent(detail.fetchImpl)).toMatchObject({
      route: "/api/v2/decision/data/record",
      init: { method: "POST" },
    });
    expect(JSON.parse(sent(detail.fetchImpl).init.body as string)).toEqual({
      kind: "decisions",
      id: EXAMPLES.data_decision.id,
    });

    const deletion = setup(reply(EXAMPLES.data_deleted));
    expect(await deletion.api.deleteData(query)).toEqual(EXAMPLES.data_deleted);
    expect(sent(deletion.fetchImpl)).toMatchObject({
      route: "/api/v2/decision/data/delete",
      init: { method: "POST" },
    });
    expect(JSON.parse(sent(deletion.fetchImpl).init.body as string)).toEqual(query);
  });

  it("validates record and delete ids before fetch", async () => {
    for (const [kind, id] of [
      ["decisions", "a"],
      ["suggestions", "../x"],
    ] as const) {
      const { api, fetchImpl } = setup(reply(EXAMPLES.data_decision));
      expect((await asError(api.dataRecord(kind, id))).code).toBe("invalid_input");
      expect(fetchImpl).not.toHaveBeenCalled();
    }
    const query: DataQuery = {
      kind: "decisions",
      ids: [EXAMPLES.data_decision.id, EXAMPLES.data_decision.id],
      topic: null,
      shown: null,
      answer: null,
      state: null,
      outcome: null,
      since: null,
      until: null,
      before: null,
    };
    const { api, fetchImpl } = setup(reply(EXAMPLES.data_deleted));
    expect((await asError(api.deleteData(query))).code).toBe("invalid_input");
    expect(fetchImpl).not.toHaveBeenCalled();
    for (const ids of [["bad"], Array.from({ length: 51 }, () => EXAMPLES.data_decision.id)]) {
      const failure = await asError(api.deleteData({ ...query, ids }));
      expect(failure.code).toBe("invalid_input");
    }
    const malformed = await asError(
      api.deleteData({ ...query, ids: "abc" as unknown as string[] }),
    );
    expect(malformed.code).toBe("invalid_input");
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it.each([
    ["Symbol", Symbol("id")],
    ["UUID-like object", { toString: () => EXAMPLES.data_decision.id }],
  ])("rejects %s record ids before fetch", async (_label, id) => {
    const record = setup(reply(EXAMPLES.data_decision));
    expect((await asError(record.api.dataRecord("decisions", id as unknown as string))).code).toBe(
      "invalid_input",
    );
    expect(record.fetchImpl).not.toHaveBeenCalled();
  });

  it.each([
    ["Symbol", Symbol("id")],
    ["UUID-like object", { toString: () => EXAMPLES.data_decision.id }],
  ])("rejects %s delete ids before fetch", async (_label, id) => {
    const query: DataQuery = {
      kind: "decisions",
      ids: [EXAMPLES.data_decision.id],
      topic: null,
      shown: null,
      answer: null,
      state: null,
      outcome: null,
      since: null,
      until: null,
      before: null,
    };
    const deletion = setup(reply(EXAMPLES.data_deleted));
    expect(
      (await asError(deletion.api.deleteData({ ...query, ids: [id] as unknown as string[] }))).code,
    ).toBe("invalid_input");
    expect(deletion.fetchImpl).not.toHaveBeenCalled();
  });

  it("GETs launcher, Bearer-authed, cache no-store, no body", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.launcher));
    const result = await api.launcher();
    expect(result).toEqual(EXAMPLES.launcher);
    const { route, init, headers } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/launcher");
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
    expect(headers).toEqual({ authorization: "Bearer tok" });
    expect(init.body).toBeUndefined();
  });

  it("POSTs launcher/open with no body and returns the launcher view", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.launcher));
    const result = await api.openLauncher();
    expect(result).toEqual(EXAMPLES.launcher);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/launcher/open");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("POSTs presence with its body verbatim and resolves void on 204", async () => {
    const body: PresenceBody = {
      window: "main",
      focused: true,
      typing: false,
      chat: null,
      chat_title: null,
      note: null,
      launcher: true,
      page: null,
    };
    const { api, fetchImpl } = setup(noBody(204));
    await expect(api.presence(body)).resolves.toBeUndefined();
    const { route, init, headers } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/presence");
    expect(init.method).toBe("POST");
    expect(headers).toEqual({ authorization: "Bearer tok", "content-type": "application/json" });
    expect(JSON.parse(init.body as string)).toEqual(body);
  });

  it("POSTs feedback verbatim and returns the launcher view", async () => {
    const body: FeedbackBody = {
      item_key: ITEM_KEY,
      episode_id: null,
      value: "useful",
      reason: null,
      source: "launcher",
    };
    const { api, fetchImpl } = setup(reply(EXAMPLES.launcher));
    const result = await api.feedback(body);
    expect(result).toEqual(EXAMPLES.launcher);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/feedback");
    expect(JSON.parse(init.body as string)).toEqual(body);
  });

  it("POSTs observe under item_keys and resolves void on 204", async () => {
    const { api, fetchImpl } = setup(noBody(204));
    await expect(api.observe("shown", [ITEM_KEY])).resolves.toBeUndefined();
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/observe");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ event: "shown", item_keys: [ITEM_KEY] });
  });

  it("POSTs reports under topic and source only (no trigger) and returns the page view", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.page));
    const result = await api.report("inbox", "launcher");
    expect(result).toEqual(EXAMPLES.page);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/reports");
    expect(JSON.parse(init.body as string)).toEqual({ topic: "inbox", source: "launcher" });
  });

  it("GETs page", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.page));
    const result = await api.page();
    expect(result).toEqual(EXAMPLES.page);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/page");
    expect(init.method).toBe("GET");
  });

  it("POSTs each preference body shape verbatim and returns the page view", async () => {
    const bodies: PreferenceBody[] = [
      { name: "unmute", candidate: "inbox" },
      { name: "review_seen" },
      { name: "reset_learning" },
    ];
    for (const body of bodies) {
      const { api, fetchImpl } = setup(reply(EXAMPLES.page));
      const result = await api.preference(body);
      expect(result).toEqual(EXAMPLES.page);
      const { route, init } = sent(fetchImpl);
      expect(route).toBe("/api/v2/decision/preferences");
      expect(JSON.parse(init.body as string)).toEqual(body);
    }
  });

  it("DELETEs episodes with no body and returns the deleted count", async () => {
    const { api, fetchImpl } = setup(reply({ deleted: 7 }));
    expect(await api.deleteEpisodes()).toEqual({ deleted: 7 });
    const { route, init, headers } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/episodes");
    expect(init.method).toBe("DELETE");
    expect(headers).toEqual({ authorization: "Bearer tok" });
    expect(init.body).toBeUndefined();
  });

  it("POSTs key with the key in the body and returns the page view", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.page));
    const result = await api.saveKey("sk-example-key");
    expect(result).toEqual(EXAMPLES.page);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/key");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ key: "sk-example-key" });
  });

  it("DELETEs key with no body and returns the page view", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.page));
    const result = await api.removeKey();
    expect(result).toEqual(EXAMPLES.page);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/key");
    expect(init.method).toBe("DELETE");
    expect(init.body).toBeUndefined();
  });

  it("POSTs test with no body and returns the test result", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.test_result));
    expect(await api.test()).toEqual(EXAMPLES.test_result);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/test");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("GETs calls", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.calls));
    expect(await api.calls()).toEqual(EXAMPLES.calls);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/calls");
    expect(init.method).toBe("GET");
  });

  it("GETs calls/{call_id} with the id encoded into the path", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.call_detail));
    expect(await api.call(CALL_ID)).toEqual(EXAMPLES.call_detail);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe(`/api/v2/decision/calls/${encodeURIComponent(CALL_ID)}`);
    expect(init.method).toBe("GET");
  });

  it("refuses a non-UUID call_id before fetching", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.call_detail));
    const failure = await asError(api.call("../episodes"));
    expect(failure).toBeInstanceOf(DecisionLoopApiError);
    expect(failure.code).toBe("invalid_input");
    expect(failure.status).toBe(0);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("GETs missions", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.page.missions));
    expect(await api.missions()).toEqual(EXAMPLES.page.missions);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/missions");
    expect(init.method).toBe("GET");
  });

  it("POSTs missions with item_key and work, returning the mission view", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.mission));
    expect(await api.startMission(ITEM_KEY, "prepare")).toEqual(EXAMPLES.mission);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/missions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ item_key: ITEM_KEY, work: "prepare" });
  });

  it("POSTs missions/{id}/stop with no body, returning the mission view", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.mission));
    expect(await api.stopMission(MISSION_ID)).toEqual(EXAMPLES.mission);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe(`/api/v2/decision/missions/${encodeURIComponent(MISSION_ID)}/stop`);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("POSTs missions/stop with no body, returning the page's missions block", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.page.missions));
    expect(await api.stopAllMissions()).toEqual(EXAMPLES.page.missions);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/missions/stop");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("POSTs seats/{id}/start and seats/{id}/stop with no body, returning the missions block", async () => {
    const routine = "802cfd61-3418-4373-8d49-3f5e5b019d92";
    for (const [name, verb] of [
      ["startSeat", "start"],
      ["stopSeat", "stop"],
    ] as const) {
      const { api, fetchImpl } = setup(reply(EXAMPLES.page.missions));
      expect(await api[name](routine)).toEqual(EXAMPLES.page.missions);
      const { route, init } = sent(fetchImpl);
      expect(route).toBe(`/api/v2/decision/seats/${routine}/${verb}`);
      expect(init.method).toBe("POST");
      expect(init.body).toBeUndefined();
    }
  });

  it("refuses a non-UUID routine id for a seat before fetching", async () => {
    for (const name of ["startSeat", "stopSeat"] as const) {
      const { api, fetchImpl } = setup(reply(EXAMPLES.page.missions));
      const failure = await asError(api[name]("../episodes"));
      expect(failure).toBeInstanceOf(DecisionLoopApiError);
      expect(failure.code).toBe("invalid_input");
      expect(failure.status).toBe(0);
      expect(fetchImpl).not.toHaveBeenCalled();
    }
  });

  it("POSTs checks/{id}/run and checks/{id}/stop with no body, returning the missions block", async () => {
    const workspace = "802cfd61-3418-4373-8d49-3f5e5b019d92";
    for (const [name, verb] of [
      ["runChecks", "run"],
      ["stopChecks", "stop"],
    ] as const) {
      const { api, fetchImpl } = setup(reply(EXAMPLES.page.missions));
      expect(await api[name](workspace)).toEqual(EXAMPLES.page.missions);
      const { route, init } = sent(fetchImpl);
      expect(route).toBe(`/api/v2/decision/checks/${workspace}/${verb}`);
      expect(init.method).toBe("POST");
      expect(init.body).toBeUndefined();
    }
  });

  it("refuses a non-UUID workspace id for checks before fetching", async () => {
    for (const name of ["runChecks", "stopChecks"] as const) {
      const { api, fetchImpl } = setup(reply(EXAMPLES.page.missions));
      const failure = await asError(api[name]("../episodes"));
      expect(failure).toBeInstanceOf(DecisionLoopApiError);
      expect(failure.code).toBe("invalid_input");
      expect(failure.status).toBe(0);
      expect(fetchImpl).not.toHaveBeenCalled();
    }
  });

  it("POSTs inbox/suggest with the item key, returning the missions block", async () => {
    const itemKey = "a".repeat(64);
    const { api, fetchImpl } = setup(reply(EXAMPLES.page.missions));
    expect(await api.suggestInbox(itemKey)).toEqual(EXAMPLES.page.missions);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/inbox/suggest");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ item_key: itemKey });
  });

  it("refuses an item key that is not 64 lowercase hex before fetching", async () => {
    for (const key of ["../episodes", "A".repeat(64), "a".repeat(63)]) {
      const { api, fetchImpl } = setup(reply(EXAMPLES.page.missions));
      const failure = await asError(api.suggestInbox(key));
      expect(failure).toBeInstanceOf(DecisionLoopApiError);
      expect(failure.code).toBe("invalid_input");
      expect(failure.status).toBe(0);
      expect(fetchImpl).not.toHaveBeenCalled();
    }
  });

  it("createProposal posts name and question", async () => {
    const { api, fetchImpl } = setup(reply({ proposal: PROPOSAL }));
    const question = { type: "noul", instructions: { question: "q", focus: "f" } };
    const result = await api.createProposal("news", question);
    expect(result).toEqual(PROPOSAL);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe("/api/v2/decision/proposals");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ name: "news", question });
  });

  it("GETs proposals/{id} with the id encoded into the path", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.question_proposal));
    expect(await api.proposal(PROPOSAL.id)).toEqual(EXAMPLES.question_proposal);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe(`/api/v2/decision/proposals/${encodeURIComponent(PROPOSAL.id)}`);
    expect(init.method).toBe("GET");
  });

  it("refuses a non-UUID proposal id before fetching", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.question_proposal));
    const failure = await asError(api.proposal("../episodes"));
    expect(failure).toBeInstanceOf(DecisionLoopApiError);
    expect(failure.code).toBe("invalid_input");
    expect(failure.status).toBe(0);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("POSTs proposals/{id} with action, revision and reason, returning the proposal", async () => {
    const { api, fetchImpl } = setup(reply({ proposal: PROPOSAL }));
    const result = await api.actProposal(
      PROPOSAL.id,
      "reject",
      PROPOSAL.revision,
      "no longer needed",
    );
    expect(result).toEqual(PROPOSAL);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe(`/api/v2/decision/proposals/${encodeURIComponent(PROPOSAL.id)}`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      action: "reject",
      revision: PROPOSAL.revision,
      reason: "no longer needed",
    });
  });

  it("POSTs proposals/{id} with a null reason when none is given", async () => {
    const { api, fetchImpl } = setup(reply({ proposal: PROPOSAL }));
    await api.actProposal(PROPOSAL.id, "approve", PROPOSAL.revision);
    const { init } = sent(fetchImpl);
    expect(JSON.parse(init.body as string)).toEqual({
      action: "approve",
      revision: PROPOSAL.revision,
      reason: null,
    });
  });

  it("actChange posts action, revision and reason to changes/{id}, returning the change act result", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.change_act));
    const result = await api.actChange(CHANGE.id, "reject", CHANGE.revision, "no longer needed");
    expect(result).toEqual(EXAMPLES.change_act);
    const { route, init } = sent(fetchImpl);
    expect(route).toBe(`/api/v2/decision/changes/${encodeURIComponent(CHANGE.id)}`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      action: "reject",
      revision: CHANGE.revision,
      reason: "no longer needed",
    });
  });

  it("actChange posts a null reason when none is given", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.change_act));
    await api.actChange(CHANGE.id, "approve", CHANGE.revision);
    const { init } = sent(fetchImpl);
    expect(JSON.parse(init.body as string)).toEqual({
      action: "approve",
      revision: CHANGE.revision,
      reason: null,
    });
  });
});

describe("Decision API response limits and shape", () => {
  it("rejects a success body over the 256 KiB cap as invalid_response", async () => {
    const { api } = setup(reply({ padding: "x".repeat(300_000) }));
    const failure = await asError(api.page());
    expect(failure.code).toBe("invalid_response");
    expect(failure.status).toBe(200);
  });

  it("rejects a 200 that is not JSON as invalid_response", async () => {
    const { api } = setup(new Response("not json", { status: 200 }));
    const failure = await asError(api.launcher());
    expect(failure.code).toBe("invalid_response");
    expect(failure.status).toBe(200);
  });

  it("rejects a 200 whose shape the launcher parser refuses as invalid_response", async () => {
    const { api } = setup(reply({ ...EXAMPLES.launcher, state: "unknown" }));
    const failure = await asError(api.launcher());
    expect(failure.code).toBe("invalid_response");
    expect(failure.status).toBe(200);
  });

  it("rejects a malformed {proposal} envelope from createProposal as invalid_response", async () => {
    const { api } = setup(reply({ proposal: { ...PROPOSAL, state: "unknown" } }));
    const failure = await asError(api.createProposal("news", {}));
    expect(failure.code).toBe("invalid_response");
    expect(failure.status).toBe(200);
  });

  it("rejects a malformed proposal view from proposal as invalid_response", async () => {
    const { api } = setup(reply({ ...EXAMPLES.question_proposal, label: 7 }));
    const failure = await asError(api.proposal(PROPOSAL.id));
    expect(failure.code).toBe("invalid_response");
    expect(failure.status).toBe(200);
  });

  it("rejects a malformed {proposal} envelope from actProposal as invalid_response", async () => {
    const { api } = setup(reply({ proposal: { ...PROPOSAL, revision: "short" } }));
    const failure = await asError(api.actProposal(PROPOSAL.id, "approve", PROPOSAL.revision));
    expect(failure.code).toBe("invalid_response");
    expect(failure.status).toBe(200);
  });

  it("rejects a malformed change envelope from actChange as invalid_response", async () => {
    const { api } = setup(
      reply({ ...EXAMPLES.change_act, change: { ...CHANGE, revision: "short" } }),
    );
    const failure = await asError(api.actChange(CHANGE.id, "approve", CHANGE.revision));
    expect(failure.code).toBe("invalid_response");
    expect(failure.status).toBe(200);
  });

  it("never attempts to read a body on a 204 route", async () => {
    const { api } = setup(noBody(204));
    const body: PresenceBody = {
      window: "w",
      focused: true,
      typing: false,
      chat: null,
      chat_title: null,
      note: null,
      launcher: false,
      page: null,
    };
    await expect(api.presence(body)).resolves.toBeUndefined();
  });
});

describe("Decision API error mapping", () => {
  it("keeps the server's parsable error code over the status table, with the real status", async () => {
    const { api } = setup(reply({ error: { code: "limit", message: "secret detail" } }, 409));
    const failure = await asError(api.startMission(ITEM_KEY, "run"));
    expect(failure).toBeInstanceOf(DecisionLoopApiError);
    expect(failure.code).toBe("limit");
    expect(failure.status).toBe(409);
    expect(failure.message).toBe("limit");
    expect(String(failure)).not.toContain("secret detail");
  });

  it.each([
    [400, "failed"],
    [401, "unauthorized"],
    [404, "unknown_item"],
    [409, "not_applicable"],
    [413, "too_large"],
    [415, "invalid_representation"],
    [429, "busy"],
    [503, "unavailable"],
    [500, "failed"],
  ])("falls back from status %d to %s when the error body is unreadable", async (status, code) => {
    const { api } = setup(new Response("not json", { status }));
    const failure = await asError(api.page());
    expect(failure.code).toBe(code);
    expect(failure.status).toBe(status);
  });

  it("falls back by status when the error body is JSON but not the {error} shape", async () => {
    const { api } = setup(reply({ oops: true }, 404));
    const failure = await asError(api.calls());
    expect(failure.code).toBe("unknown_item");
    expect(failure.status).toBe(404);
  });
});

describe("Decision API data cancellation and busy responses", () => {
  it("passes abort signals to data reads and maps 429 to busy", async () => {
    const controller = new AbortController();
    controller.abort();
    const { api, fetchImpl } = setup(reply(EXAMPLES.data_summary));
    expect((await asError(api.dataSummary(controller.signal))).code).toBe("cancelled");
    expect(fetchImpl).not.toHaveBeenCalled();
    const busy = createDecisionLoopApi(
      "tok",
      vi.fn(async () => reply({}, 429)) as unknown as typeof fetch,
    );
    expect(
      (
        await asError(
          busy.dataRecords({
            kind: "decisions",
            ids: null,
            topic: null,
            shown: null,
            answer: null,
            state: null,
            outcome: null,
            since: null,
            until: null,
            before: null,
          }),
        )
      ).code,
    ).toBe("busy");
  });
});

describe("Decision API timeout and abort", () => {
  it("aborts a read at 15 s, not before, with a timeout code", async () => {
    vi.useFakeTimers();
    let failure: DecisionLoopApiError | undefined;
    createDecisionLoopApi("tok", hangingFetch())
      .page()
      .catch((error: DecisionLoopApiError) => {
        failure = error;
      });
    await vi.advanceTimersByTimeAsync(14_999);
    expect(failure).toBeUndefined();
    await vi.advanceTimersByTimeAsync(1);
    expect(failure?.code).toBe("timeout");
    expect(failure?.status).toBe(0);
  });

  it("aborts a write route with no caller signal at 15 s too", async () => {
    vi.useFakeTimers();
    let failure: DecisionLoopApiError | undefined;
    createDecisionLoopApi("tok", hangingFetch())
      .test()
      .catch((error: DecisionLoopApiError) => {
        failure = error;
      });
    await vi.advanceTimersByTimeAsync(15_000);
    expect(failure?.code).toBe("timeout");
    expect(failure?.status).toBe(0);
  });

  it("refuses an already-aborted call before fetching, at status 0", async () => {
    const { api, fetchImpl } = setup(reply(EXAMPLES.launcher));
    const controller = new AbortController();
    controller.abort();
    const failure = await asError(api.launcher(controller.signal));
    expect(failure.code).toBe("cancelled");
    expect(failure.status).toBe(0);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("cancels an in-flight call when the caller's own signal aborts", async () => {
    const controller = new AbortController();
    const api = createDecisionLoopApi("tok", hangingFetch());
    const pending = asError(api.calls(controller.signal));
    controller.abort();
    const failure = await pending;
    expect(failure.code).toBe("cancelled");
    expect(failure.status).toBe(0);
  });
});

describe("Decision API key secrecy (saveKey)", () => {
  it("sends the key once in that call's body and keeps it nowhere else after it resolves", async () => {
    const KEY = "sk-marker-should-not-persist-anywhere-else";
    // A fresh Response per call (a body can be read once), matching each call's own expected shape.
    const fetchImpl = vi.fn();
    fetchImpl.mockImplementationOnce(async () => reply(EXAMPLES.page));
    fetchImpl.mockImplementationOnce(async () => reply(EXAMPLES.launcher));
    const api = createDecisionLoopApi("tok", fetchImpl as unknown as typeof fetch);
    const result = await api.saveKey(KEY);
    expect(JSON.stringify(result)).not.toContain(KEY);

    // The api object carries only methods: no field anywhere could be holding the key.
    for (const value of Object.values(api)) expect(typeof value).toBe("function");

    // The one call's body carried it; nothing else about that call's init does.
    const { init } = sent(fetchImpl);
    expect(JSON.parse(init.body as string)).toEqual({ key: KEY });
    expect(JSON.stringify({ ...init, body: undefined })).not.toContain(KEY);

    // A later, unrelated call neither carries nor echoes the key.
    await api.launcher();
    const second = sent(fetchImpl, 1);
    expect(JSON.stringify(second.init)).not.toContain(KEY);
  });

  it("an error thrown by saveKey carries no key text, even if the server echoes it back", async () => {
    const KEY = "sk-marker-should-never-leak-in-an-error";
    const { api } = setup(
      reply({ error: { code: "invalid_request", message: `rejected key ${KEY}` } }, 400),
    );
    const failure = await asError(api.saveKey(KEY));
    expect(failure).toBeInstanceOf(DecisionLoopApiError);
    expect(failure.code).toBe("invalid_request");
    expect(failure.message).toBe("invalid_request");
    expect(String(failure)).not.toContain(KEY);
    expect(JSON.stringify(Object.getOwnPropertyNames(failure))).not.toContain(KEY);
  });

  it("a malformed response to saveKey still carries no key text", async () => {
    const KEY = "sk-marker-in-malformed-path";
    const { api } = setup(reply({ not: "a page view" }));
    const failure = await asError(api.saveKey(KEY));
    expect(failure.code).toBe("invalid_response");
    expect(String(failure)).not.toContain(KEY);
  });
});
