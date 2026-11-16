/**
 * The browser half of the durable approval ledger: what this page may conclude about a
 * recorded choice, and what it must never do — answer twice, answer the opposite way, or
 * replace an uncertain POST with a second one under a new ID. No timers: every case here is
 * decided by promises the fake settles.
 */
import { afterEach, expect, test, vi } from "vitest";
import {
  type ApprovalInput,
  type ApprovalReceipt,
  type ApprovalRecord,
  type DurableApi,
  DurableApiError,
} from "../clients/conversations/durableApi";
import { type ApprovalPhase, createApprovalStore } from "./approvals";
import type { Card } from "./view";

const SESSION = "11111111-1111-4111-8111-111111111111";
const GENERATION = "22222222-2222-4222-8222-222222222222";
const RUNTIME = "33333333-3333-4333-8333-333333333333";
const REQUEST = "44444444-4444-4444-8444-444444444444";
const TURN = "55555555-5555-4555-8555-555555555555";
const CHOICE = "66666666-6666-4666-8666-666666666666";
const AT = "2026-09-16T00:00:00.000Z";
const KEY = `viki.approval.pending.${REQUEST}`;

afterEach(() => {
  vi.useRealTimers();
});

function card(): Card {
  return {
    kind: "permission",
    data: {
      tool_use_id: "tu-1",
      kind: "tool",
      tool: "Write",
      summary: "30-knowledge/x.md",
      question: "May I rewrite this file?",
      reason: "",
      agent: "",
      plan: "",
    },
    nativeRequest: { runtime_id: RUNTIME, request_id: REQUEST, turn_id: TURN },
  };
}

function receipt(over: Partial<ApprovalReceipt> = {}): ApprovalReceipt {
  return {
    choice_id: CHOICE,
    session_id: SESSION,
    generation: GENERATION,
    runtime_id: RUNTIME,
    request_id: REQUEST,
    turn_id: TURN,
    tool_use_id: "tu-1",
    state: "delivered_locally",
    reason: "",
    created_at: AT,
    updated_at: AT,
    ...over,
  };
}

function record(allow = true, over: Partial<ApprovalReceipt> = {}): ApprovalRecord {
  return {
    ...receipt(over),
    choice: { kind: "permission", allow },
    request: {
      type: "permission.requested",
      data: {
        tool_use_id: "tu-1",
        kind: "tool",
        tool: "Write",
        summary: "30-knowledge/x.md",
        question: "",
        reason: "",
        agent: "",
        plan: "",
      },
    },
  };
}

function memoryStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const data = new Map<string, string>();
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, value);
    },
    removeItem: (key) => {
      data.delete(key);
    },
  };
}

function fakeApi(overrides: Partial<DurableApi> = {}): DurableApi {
  return {
    getDraft: vi.fn(),
    listDrafts: vi.fn(),
    saveDraft: vi.fn(),
    clearDraft: vi.fn(),
    getTarget: vi.fn(),
    getDraftCommand: vi.fn(),
    submitCommand: vi.fn(),
    getCommand: vi.fn(),
    listCommands: vi.fn(),
    getSubmission: vi.fn(),
    findApproval: vi.fn(async () => null),
    recordApproval: vi.fn(async (input: ApprovalInput) => receipt({ choice_id: input.choice_id })),
    getApproval: vi.fn(async () => receipt()),
    getApprovalChoice: vi.fn(async () => record()),
    listApprovals: vi.fn(),
    ...overrides,
  };
}

/** Drain the microtask queue: the store's own chains are awaits, never timers. */
async function settled(): Promise<void> {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
}

function phaseOf(store: ReturnType<typeof createApprovalStore>): ApprovalPhase | undefined {
  return store.getSnapshot().approvals[REQUEST]?.phase;
}

test("a card observed without a retained id adopts an existing record", async () => {
  const findApproval = vi.fn(async () => receipt({ state: "delivering" }));
  const api = fakeApi({ findApproval });
  const store = createApprovalStore(api, { storage: memoryStorage() });
  store.observe(card(), SESSION, GENERATION);
  await settled();

  expect(findApproval).toHaveBeenCalledWith(SESSION, RUNTIME, REQUEST);
  expect(store.getSnapshot().approvals[REQUEST]).toMatchObject({
    phase: "recorded",
    choice: { kind: "permission", allow: true },
    receipt: { state: "delivering" },
  });
  expect(api.recordApproval).not.toHaveBeenCalled();
});

test("a recorded request ignores decide, and the opposite choice never replaces it", async () => {
  const api = fakeApi({ findApproval: vi.fn(async () => receipt()) });
  const store = createApprovalStore(api, { storage: memoryStorage() });
  store.observe(card(), SESSION, GENERATION);
  await settled();

  const outcome = await store.decide(card(), SESSION, GENERATION, {
    kind: "permission",
    allow: false,
  });
  expect(outcome).toBe("applied");
  expect(api.recordApproval).not.toHaveBeenCalled();
  expect(store.getSnapshot().approvals[REQUEST]?.choice).toEqual({
    kind: "permission",
    allow: true,
  });
});

test("a click during the first lookup waits for it instead of posting a second choice", async () => {
  let arrive: (value: ApprovalReceipt | null) => void = () => {};
  const api = fakeApi({
    findApproval: vi.fn(
      () =>
        new Promise<ApprovalReceipt | null>((resolve) => {
          arrive = resolve;
        }),
    ),
  });
  const store = createApprovalStore(api, { storage: memoryStorage() });
  store.observe(card(), SESSION, GENERATION);
  expect(phaseOf(store)).toBe("checking");

  const clicked = store.decide(card(), SESSION, GENERATION, { kind: "permission", allow: true });
  arrive(receipt());
  expect(await clicked).toBe("applied");
  expect(api.recordApproval).not.toHaveBeenCalled();
});

test("a 409 already_decided adopts the other window's record without an interrupted state", async () => {
  const findApproval = vi
    .fn<DurableApi["findApproval"]>()
    .mockResolvedValueOnce(null)
    .mockResolvedValue(receipt());
  const recordApproval = vi.fn(async () => {
    throw new DurableApiError(409, "already_decided");
  });
  const storage = memoryStorage();
  const store = createApprovalStore(fakeApi({ findApproval, recordApproval }), { storage });
  const phases: (ApprovalPhase | undefined)[] = [];
  store.subscribe(() => phases.push(phaseOf(store)));
  store.observe(card(), SESSION, GENERATION);
  await settled();

  const outcome = await store.decide(card(), SESSION, GENERATION, {
    kind: "permission",
    allow: true,
  });
  await settled();
  expect(outcome).toBe("applied");
  expect(phaseOf(store)).toBe("recorded");
  expect(store.getSnapshot().approvals[REQUEST]?.refusal).toBeNull();
  expect(phases).not.toContain("refused");
  expect(phases).not.toContain("ambiguous");
  expect(recordApproval).toHaveBeenCalledTimes(1);
  expect(storage.getItem(KEY)).toBeNull();
});

test("a retained id is inspected on load, on synced and on click, never re-posted", async () => {
  const storage = memoryStorage();
  storage.setItem(
    KEY,
    JSON.stringify({
      choice_id: CHOICE,
      session_id: SESSION,
      generation: GENERATION,
      tool_use_id: "tu-1",
      native_request: { runtime_id: RUNTIME, request_id: REQUEST, turn_id: TURN },
    }),
  );
  const getApproval = vi
    .fn<DurableApi["getApproval"]>()
    .mockRejectedValueOnce(new DurableApiError(503, "unavailable"))
    .mockRejectedValueOnce(new DurableApiError(503, "unavailable"))
    .mockResolvedValue(receipt({ state: "unknown", reason: "interrupted" }));
  const api = fakeApi({ getApproval });
  const store = createApprovalStore(api, { storage });

  store.observe(card(), SESSION, GENERATION); // page load
  await settled();
  expect(phaseOf(store)).toBe("ambiguous");
  expect(getApproval).toHaveBeenCalledTimes(1);

  store.resynced(SESSION); // session.synced
  await settled();
  expect(phaseOf(store)).toBe("ambiguous");
  expect(getApproval).toHaveBeenCalledTimes(2);

  const outcome = await store.decide(card(), SESSION, GENERATION, {
    kind: "permission",
    allow: false,
  });
  await settled();
  expect(getApproval).toHaveBeenCalledTimes(3);
  expect(outcome).toBe("applied");
  expect(store.getSnapshot().approvals[REQUEST]).toMatchObject({
    phase: "recorded",
    receipt: { state: "unknown", reason: "interrupted" },
  });
  // The whole point: no second attempt, and nothing looked up by request either — the retained
  // ID is the only thing this page may ask about.
  expect(api.recordApproval).not.toHaveBeenCalled();
  expect(api.findApproval).not.toHaveBeenCalled();
  expect(storage.getItem(KEY)).toBeNull();
});

test("a confirmed 404 releases the retained id and the next click posts a fresh one", async () => {
  const storage = memoryStorage();
  storage.setItem(
    KEY,
    JSON.stringify({
      choice_id: CHOICE,
      session_id: SESSION,
      generation: GENERATION,
      tool_use_id: "tu-1",
      native_request: { runtime_id: RUNTIME, request_id: REQUEST, turn_id: TURN },
    }),
  );
  const recordApproval = vi.fn(async (input: ApprovalInput) =>
    receipt({ choice_id: input.choice_id }),
  );
  const store = createApprovalStore(
    fakeApi({
      getApproval: vi.fn(async () => {
        throw new DurableApiError(404, "not_found");
      }),
      recordApproval,
    }),
    { storage },
  );
  store.observe(card(), SESSION, GENERATION);
  await settled();
  expect(phaseOf(store)).toBe("idle");
  expect(storage.getItem(KEY)).toBeNull();

  const outcome = await store.decide(card(), SESSION, GENERATION, {
    kind: "permission",
    allow: true,
  });
  expect(outcome).toBe("applied");
  expect(recordApproval).toHaveBeenCalledTimes(1);
  expect(recordApproval.mock.calls[0]?.[0]?.choice_id).not.toBe(CHOICE);
});

test("an ambiguous post retains one choice id and blocks a second choice", async () => {
  const storage = memoryStorage();
  const recordApproval = vi.fn(async () => {
    throw new DurableApiError(0, "network_error");
  });
  const getApproval = vi.fn(async () => {
    throw new DurableApiError(503, "unavailable");
  });
  const store = createApprovalStore(fakeApi({ recordApproval, getApproval }), { storage });
  store.observe(card(), SESSION, GENERATION);
  await settled();

  expect(await store.decide(card(), SESSION, GENERATION, { kind: "permission", allow: true })).toBe(
    "unknown",
  );
  expect(phaseOf(store)).toBe("ambiguous");
  const retained = JSON.parse(String(storage.getItem(KEY)));
  expect(retained.choice_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(retained.choice).toBeUndefined(); // identifiers only: no choice body in browser storage

  // The opposite answer, immediately afterwards: it inspects the retained ID and records nothing.
  expect(
    await store.decide(card(), SESSION, GENERATION, { kind: "permission", allow: false }),
  ).toBe("unknown");
  expect(recordApproval).toHaveBeenCalledTimes(1);
  expect(getApproval).toHaveBeenCalledTimes(1);
  expect(JSON.parse(String(storage.getItem(KEY))).choice_id).toBe(retained.choice_id);
});

test("a capacity refusal is distinct from a save failure, but still releases the latch with a fresh id", async () => {
  const storage = memoryStorage();
  const recordApproval = vi
    .fn<DurableApi["recordApproval"]>()
    .mockRejectedValueOnce(new DurableApiError(429, "capacity"))
    .mockImplementation(async (input) => receipt({ choice_id: input.choice_id }));
  const store = createApprovalStore(fakeApi({ recordApproval }), { storage });
  store.observe(card(), SESSION, GENERATION);
  await settled();

  expect(await store.decide(card(), SESSION, GENERATION, { kind: "permission", allow: true })).toBe(
    "rejected",
  );
  expect(store.getSnapshot().approvals[REQUEST]).toMatchObject({
    phase: "refused",
    refusal: "capacity",
  });
  expect(storage.getItem(KEY)).toBeNull();
});

test.each([
  ["protection", 503],
  ["too_large", 413],
])("a definite %s refusal releases the latch with a fresh id", async (code, status) => {
  const storage = memoryStorage();
  const recordApproval = vi
    .fn<DurableApi["recordApproval"]>()
    .mockRejectedValueOnce(new DurableApiError(status, code))
    .mockImplementation(async (input) => receipt({ choice_id: input.choice_id }));
  const store = createApprovalStore(fakeApi({ recordApproval }), { storage });
  store.observe(card(), SESSION, GENERATION);
  await settled();

  expect(await store.decide(card(), SESSION, GENERATION, { kind: "permission", allow: true })).toBe(
    "rejected",
  );
  expect(store.getSnapshot().approvals[REQUEST]).toMatchObject({
    phase: "refused",
    refusal: "save",
  });
  expect(storage.getItem(KEY)).toBeNull();

  expect(await store.decide(card(), SESSION, GENERATION, { kind: "permission", allow: true })).toBe(
    "applied",
  );
  const [first, second] = recordApproval.mock.calls;
  expect(first?.[0]?.choice_id).not.toBe(second?.[0]?.choice_id);
});

test.each(["stale_request", "stale_generation", "closed"])(
  "a %s refusal says the request is over and records nothing",
  async (code) => {
    const storage = memoryStorage();
    const store = createApprovalStore(
      fakeApi({
        recordApproval: vi.fn(async () => {
          throw new DurableApiError(409, code);
        }),
      }),
      { storage },
    );
    store.observe(card(), SESSION, GENERATION);
    await settled();

    expect(
      await store.decide(card(), SESSION, GENERATION, { kind: "permission", allow: true }),
    ).toBe("rejected");
    expect(store.getSnapshot().approvals[REQUEST]).toMatchObject({
      phase: "refused",
      refusal: "over",
    });
    expect(storage.getItem(KEY)).toBeNull();
  },
);

test("a question answer records its answers map, and forget drops the entry and its key", async () => {
  const storage = memoryStorage();
  const recordApproval = vi.fn(async (input: ApprovalInput) =>
    receipt({ choice_id: input.choice_id }),
  );
  const store = createApprovalStore(fakeApi({ recordApproval }), { storage });
  const asked: Card = {
    kind: "question",
    data: { tool_use_id: "tu-q", questions: [] },
    nativeRequest: { runtime_id: RUNTIME, request_id: REQUEST, turn_id: TURN },
  };
  store.observe(asked, SESSION, GENERATION);
  await settled();
  await store.decide(asked, SESSION, GENERATION, {
    kind: "question",
    answers: { "Where should it go?": "30-knowledge" },
    response: "",
  });

  expect(recordApproval.mock.calls[0]?.[0]).toMatchObject({
    session_id: SESSION,
    generation: GENERATION,
    tool_use_id: "tu-q",
    native_request: { runtime_id: RUNTIME, request_id: REQUEST, turn_id: TURN },
    choice: { kind: "question", answers: { "Where should it go?": "30-knowledge" }, response: "" },
  });

  store.forget(REQUEST);
  expect(store.getSnapshot().approvals[REQUEST]).toBeUndefined();
  expect(storage.getItem(KEY)).toBeNull();
});

test("a card with no native binding is never answered at all", async () => {
  const api = fakeApi();
  const store = createApprovalStore(api, { storage: memoryStorage() });
  const unbound: Card = { ...card(), nativeRequest: null };
  store.observe(unbound, SESSION, GENERATION);
  expect(
    await store.decide(unbound, SESSION, GENERATION, { kind: "permission", allow: true }),
  ).toBe("rejected");
  expect(api.findApproval).not.toHaveBeenCalled();
  expect(api.recordApproval).not.toHaveBeenCalled();
});

test("a forgotten card cannot be re-added by its own POST landing later", async () => {
  // The normal ordering when another window answers first: request.answered closes the card (App
  // forgets the entry) while this page's POST is still in flight.
  let land: (receipt: ApprovalReceipt) => void = () => {};
  const storage = memoryStorage();
  const store = createApprovalStore(
    fakeApi({
      recordApproval: vi.fn(
        () =>
          new Promise<ApprovalReceipt>((resolve) => {
            land = resolve;
          }),
      ),
    }),
    { storage },
  );
  store.observe(card(), SESSION, GENERATION);
  await settled();
  const answering = store.decide(card(), SESSION, GENERATION, { kind: "permission", allow: true });

  store.forget(REQUEST);
  expect(Object.keys(store.getSnapshot().approvals)).toEqual([]);
  expect(storage.getItem(KEY)).toBeNull();

  land(receipt());
  await answering;
  await settled();
  expect(Object.keys(store.getSnapshot().approvals)).toEqual([]);
});
