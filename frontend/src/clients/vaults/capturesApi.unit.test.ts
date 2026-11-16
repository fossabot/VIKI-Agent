import { describe, expect, it, vi } from "vitest";
import {
  type CaptureListing,
  CapturesApiError,
  createCapturesApi,
  DESTINATIONS,
} from "./capturesApi";

const HASH = "a".repeat(64);
const SID = "00000000-0000-4000-8000-000000000001";
const AT = "2026-10-02T03:04:05.678Z";
const FINISHED = "2026-10-02T03:10:05.678Z";

const suggestionFx = (over: Record<string, unknown> = {}) => ({
  to: "task",
  project: "viki-agent",
  why: "mentions the roadmap",
  at: AT,
  ...over,
});
const captureFx = (over: Record<string, unknown> = {}) => ({
  ref: "00-inbox/2026-10-09-note.md",
  revision: HASH,
  modified: AT,
  captured: AT,
  preview: "a thought",
  cut: false,
  unread: null,
  suggestion: null,
  ...over,
});
const runFx = (over: Record<string, unknown> = {}) => ({
  run_id: "run-1",
  session_id: SID,
  state: "done",
  started_at: AT,
  finished_at: FINISHED,
  reason: null,
  suggested: 2,
  rejected: 1,
  ...over,
});
const listingFx = (over: Record<string, unknown> = {}) => ({
  captures: [captureFx()],
  projects: ["viki-agent"],
  partial: false,
  omitted: 0,
  run: null,
  ...over,
});
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function setup(response: Response) {
  const fetchImpl = vi.fn(async () => response);
  return { fetchImpl, api: createCapturesApi("tok", fetchImpl as unknown as typeof fetch) };
}
const sent = (fetchImpl: ReturnType<typeof vi.fn>) => {
  const [route, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
  return { route, init, headers: init.headers as Record<string, string> };
};
const invalid = async (promise: Promise<unknown>) =>
  expect(((await promise.catch((error) => error)) as CapturesApiError).code).toBe(
    "invalid_response",
  );

describe("captures API paths and bodies", () => {
  it("exports the seven destinations in order", () => {
    expect(DESTINATIONS).toEqual([
      "knowledge",
      "source",
      "task",
      "area",
      "person",
      "daily",
      "discard",
    ]);
  });

  it("GETs the listing with a Bearer token and no body or Origin", async () => {
    const { api, fetchImpl } = setup(reply(listingFx()));
    const listing = await api.list("ws1");
    expect(listing.captures).toHaveLength(1);
    const { route, init, headers } = sent(fetchImpl);
    expect(route).toBe("/api/v2/vaults/ws1/captures");
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect(headers).toEqual({ authorization: "Bearer tok" });
  });

  it("encodeURIComponents the id on every route", async () => {
    const listing = setup(reply(listingFx()));
    await listing.api.list("w s/1");
    expect(sent(listing.fetchImpl).route).toBe("/api/v2/vaults/w%20s%2F1/captures");

    const current = setup(reply({ run: null }));
    await current.api.run("w s/1");
    expect(sent(current.fetchImpl).route).toBe("/api/v2/vaults/w%20s%2F1/captures/suggest");

    const started = setup(reply({ run: runFx({ state: "running", finished_at: null }) }, 202));
    await started.api.suggest("w s/1");
    expect(sent(started.fetchImpl).route).toBe("/api/v2/vaults/w%20s%2F1/captures/suggest");
  });

  it("GETs the current run, null or present, without a body", async () => {
    const empty = setup(reply({ run: null }));
    expect(await empty.api.run("ws1")).toBeNull();
    const { route, init, headers } = sent(empty.fetchImpl);
    expect(route).toBe("/api/v2/vaults/ws1/captures/suggest");
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect(headers).toEqual({ authorization: "Bearer tok" });

    const present = setup(reply({ run: runFx({ state: "running", finished_at: null }) }));
    const found = await present.api.run("ws1");
    expect(found?.state).toBe("running");
  });

  it("POSTs suggest with an exact empty body and reads the 202 run", async () => {
    const { api, fetchImpl } = setup(
      reply({ run: runFx({ state: "running", finished_at: null }) }, 202),
    );
    const run = await api.suggest("ws1");
    expect(run.state).toBe("running");
    const { route, init, headers } = sent(fetchImpl);
    expect(route).toBe("/api/v2/vaults/ws1/captures/suggest");
    expect(init.method).toBe("POST");
    expect(headers).toEqual({ authorization: "Bearer tok", "content-type": "application/json" });
    expect(init.body).toBe("{}");
  });
});

describe("captures API failures", () => {
  it("carries the server's code and details", async () => {
    const { api } = setup(
      reply(
        { error: { code: "suggestion_running", message: "m", details: { run_id: "r1" } } },
        409,
      ),
    );
    const failure = await api.suggest("ws1").catch((error) => error);
    expect(failure).toBeInstanceOf(CapturesApiError);
    expect(failure.code).toBe("suggestion_running");
    expect(failure.extra).toEqual({ run_id: "r1" });
  });

  it.each([
    [413, "too_large"],
    [415, "invalid_representation"],
    [404, "unknown_workspace"],
  ])("maps %i to its code", async (status, code) => {
    const { api } = setup(reply({ error: { code, message: "m" } }, status));
    const failure = await api.list("ws1").catch((error) => error);
    expect(failure.code).toBe(code);
    expect(failure.extra).toEqual({});
  });

  it("falls back to unavailable for an unreadable error body", async () => {
    const { api } = setup(new Response("<html>", { status: 502 }));
    expect((await api.list("ws1").catch((error) => error)).code).toBe("unavailable");
  });

  it("refuses a 200 that is not JSON", async () => {
    await invalid(setup(new Response("nope", { status: 200 })).api.list("ws1"));
  });

  it("refuses an aborted call before fetching", async () => {
    const { api, fetchImpl } = setup(reply(listingFx()));
    const controller = new AbortController();
    controller.abort();
    expect((await api.list("ws1", controller.signal).catch((error) => error)).code).toBe(
      "cancelled",
    );
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("captures API reply validation", () => {
  it("reads a fully populated listing: bound, unbound and non-task captures, a finished run", async () => {
    const full: CaptureListing = listingFx({
      captures: [
        captureFx({ suggestion: suggestionFx() }),
        captureFx({
          ref: "00-inbox/other.md",
          revision: null,
          cut: true,
          unread: "3 new",
          suggestion: null,
        }),
        captureFx({
          ref: "00-inbox/third.md",
          suggestion: suggestionFx({ to: "discard", project: null, why: "" }),
        }),
      ],
      projects: ["viki-agent", "viki-vault"],
      run: runFx({ state: "failed", reason: "crashed" }),
    }) as CaptureListing;
    const { api } = setup(reply(full));
    expect(await api.list("ws1")).toEqual(full);
  });

  it("accepts fields at their exact length caps", async () => {
    const atCap = listingFx({
      captures: [
        captureFx({
          ref: "a",
          captured: "a".repeat(64),
          preview: "a".repeat(600),
          unread: "a".repeat(64),
          suggestion: suggestionFx({ why: "a".repeat(200) }),
        }),
        captureFx({ ref: "a".repeat(512) }),
      ],
      run: runFx({ reason: "a".repeat(64) }),
    });
    const { api } = setup(reply(atCap));
    expect(await api.list("ws1")).toEqual(atCap);
  });

  it("accepts a preview at the cap counted in code points, astral character included", async () => {
    // One surrogate pair (2 UTF-16 units, 1 code point) plus 599 ASCII chars: 600 code points,
    // the server's own cap, but 601 UTF-16 units — a naive `.length` cap would refuse it.
    const withAstral = listingFx({
      captures: [captureFx({ preview: `📌${"a".repeat(599)}` })],
    });
    const { api } = setup(reply(withAstral));
    expect(await api.list("ws1")).toEqual(withAstral);
  });

  it("accepts a ref at the cap counted in code points, astral character included", async () => {
    // One surrogate pair (2 UTF-16 units, 1 code point) plus 511 ASCII chars: 512 code points,
    // the server's own cap, but 513 UTF-16 units — a naive `.length` cap would refuse it.
    const withRefAstral = listingFx({
      captures: [captureFx({ ref: `📌${"a".repeat(511)}` })],
    });
    const { api } = setup(reply(withRefAstral));
    expect(await api.list("ws1")).toEqual(withRefAstral);
  });

  it("accepts a suggestion whose why is null", async () => {
    const noWhy = listingFx({
      captures: [captureFx({ suggestion: suggestionFx({ why: null }) })],
    });
    const { api } = setup(reply(noWhy));
    expect(await api.list("ws1")).toEqual(noWhy);
  });

  it.each([
    ["empty ref", { ref: "" }],
    ["too-long ref", { ref: "a".repeat(513) }],
    ["non-hex revision", { revision: "z".repeat(64) }],
    ["short revision", { revision: "a".repeat(63) }],
    ["bad modified stamp", { modified: "2026-10-09" }],
    ["too-long captured", { captured: "a".repeat(65) }],
    ["too-long preview", { preview: "a".repeat(601) }],
    ["non-boolean cut", { cut: "yes" }],
    ["too-long unread", { unread: "a".repeat(65) }],
    ["an unknown suggestion destination", { suggestion: suggestionFx({ to: "inbox" }) }],
    ["a task suggestion missing a project", { suggestion: suggestionFx({ project: null }) }],
    [
      "a non-task suggestion carrying a project",
      { suggestion: suggestionFx({ to: "daily", project: "p" }) },
    ],
    ["a too-long suggestion why", { suggestion: suggestionFx({ why: "a".repeat(201) }) }],
    ["a bad suggestion stamp", { suggestion: suggestionFx({ at: "not-a-stamp" }) }],
  ])("refuses a capture with %s", async (_label, over) => {
    await invalid(setup(reply(listingFx({ captures: [captureFx(over)] }))).api.list("ws1"));
  });

  it("refuses more than 100 captures", async () => {
    const captures = Array.from({ length: 101 }, () => captureFx());
    await invalid(setup(reply(listingFx({ captures }))).api.list("ws1"));
  });

  it.each([
    ["a non-string project", { projects: [1] }],
    ["a non-boolean partial", { partial: "false" }],
    ["a negative omitted", { omitted: -1 }],
    ["a fractional omitted", { omitted: 1.5 }],
  ])("refuses a listing with %s", async (_label, over) => {
    await invalid(setup(reply(listingFx(over))).api.list("ws1"));
  });

  it.each([
    ["a non-string run_id", { run_id: 1 }],
    ["a non-uuid session_id", { session_id: "not-a-uuid" }],
    ["an unknown state", { state: "queued" }],
    ["a bad started_at", { started_at: "2026-10-09" }],
    ["a bad finished_at", { finished_at: "2026-10-09" }],
    ["a too-long reason", { reason: "a".repeat(65) }],
    ["a negative suggested count", { suggested: -1 }],
    ["a fractional rejected count", { rejected: 0.5 }],
  ])("refuses a run with %s, from the listing, the poll and the start", async (_label, over) => {
    const bad = runFx(over);
    await invalid(setup(reply(listingFx({ run: bad }))).api.list("ws1"));
    await invalid(setup(reply({ run: bad })).api.run("ws1"));
    await invalid(setup(reply({ run: bad }, 202)).api.suggest("ws1"));
  });

  it("refuses a run reply with no run field at all", async () => {
    await invalid(setup(reply({})).api.run("ws1"));
    await invalid(setup(reply({}, 202)).api.suggest("ws1"));
  });
});
