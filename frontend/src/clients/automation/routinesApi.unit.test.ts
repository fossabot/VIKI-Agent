import { afterEach, expect, test, vi } from "vitest";
import { createRoutinesApi, type RoutineDocument } from "./routinesApi";

const ID = "11111111-1111-4111-8111-111111111111";
const COMMAND = "22222222-2222-4222-8222-222222222222";

const document: RoutineDocument = {
  name: "Morning",
  purpose: "",
  prompt: "Hello",
  profile_id: "33333333-3333-4333-8333-333333333333",
  workspace_id: "44444444-4444-4444-8444-444444444444",
  deadline_minutes: 30,
  schedule: {
    kind: "manual",
    times: [],
    days: [],
    timezone: null,
    start_date: null,
    end_date: null,
    catch_up: "skip",
  },
};
const attempt = {
  attempt_id: "a1",
  session_id: "s1",
  state: "done",
  outcome: "succeeded",
  reason: null,
  profile_id: null,
  profile_revision: 1,
  native_version: null,
  created_at: "2026-10-03T00:00:00.000Z",
  dispatched_at: null,
  finished_at: null,
};
const run = {
  occurrence_id: "o1",
  kind: "manual",
  scheduled_for: null,
  trigger_id: null,
  revision: 1,
  state: "done",
  reason: null,
  recorded_at: "2026-10-03T00:00:00.000Z",
  attempt,
};
const item = {
  routine_id: ID,
  name: "Morning",
  state: "enabled",
  current_revision: 2,
  state_revision: 3,
  schedule: { kind: "manual", timezone: null },
  next_run: null,
  last_run: run,
  last_attempt: run,
  active_run: { attempt_id: "a2", session_id: "s2", state: "running", stop_by: null },
};
const preview = {
  next_five: [
    {
      at: null,
      local: "2026-03-29T02:30",
      zone: "Europe/Paris",
      offset_minutes: null,
      note: "dst_gap",
    },
  ],
  readiness: { blockers: [], run_blockers: [], warnings: ["workspace_not_active"] },
  resolved: { profile: null, workspace: null, first_turn: null, deadline_minutes: 30 },
};
const detail = { ...item, ...preview, document, tested_revision: null, activates_at: "x" };
const change = { routine_id: ID, revision: 2, state: "draft", state_revision: 1 };

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

afterEach(() => vi.useRealTimers());

test("list sends the Bearer token with no body and parses items and scheduler", async () => {
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
    reply({ routines: [item], scheduler: { state: "running", attended: true, paused: false } }),
  );
  const result = await createRoutinesApi("tok", fetchImpl).list();
  expect(result.routines[0]?.active_run?.stop_by).toBeNull();
  expect(result.scheduler.paused).toBe(false);
  const [url, init] = fetchImpl.mock.calls[0] ?? [];
  expect(url).toBe("/api/v2/routines");
  expect(init?.method).toBe("GET");
  expect(init?.headers).toEqual({ authorization: "Bearer tok" });
  expect(init?.body).toBeUndefined();
});

test("get encodes the id and parses the detail fields", async () => {
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL) => reply(detail));
  const result = await createRoutinesApi("tok", fetchImpl).get("a/b?c");
  expect(fetchImpl.mock.calls[0]?.[0]).toBe("/api/v2/routines/a%2Fb%3Fc");
  expect(result.document.name).toBe("Morning");
  expect(result.readiness.warnings).toEqual(["workspace_not_active"]);
  expect(result.next_five[0]?.note).toBe("dst_gap");
});

test("next_five accepts the server's slots: note is empty for an ordinary one, offset null for a gap", async () => {
  const slots = [
    {
      at: "2026-10-04T00:30:00.000Z",
      local: "2026-10-04T09:30+09:00",
      zone: "Asia/Tokyo",
      offset_minutes: 540,
      note: "",
    },
    {
      at: "2026-11-01T05:30:00.000Z",
      local: "2026-11-01T01:30-04:00",
      zone: "America/New_York",
      offset_minutes: -240,
      note: "dst_fold",
    },
    {
      at: null,
      local: "2026-03-08T02:30",
      zone: "America/New_York",
      offset_minutes: null,
      note: "dst_gap",
    },
  ];
  const fetchImpl = vi.fn(async () => reply({ ...detail, next_five: slots }));
  const result = await createRoutinesApi("tok", fetchImpl).get(ID);
  expect(result.next_five.map((slot) => slot.note)).toEqual([null, "dst_fold", "dst_gap"]);
  expect(result.next_five.map((slot) => slot.offset_minutes)).toEqual([540, -240, null]);
  expect(result.next_five[2]?.at).toBeNull();
});

// Bodies as tests/automation/test_routines_api.py asserts them.
const SERVER_ID = "5b0c2d3e-1111-4222-8333-444455556666";
const serverSlots = [
  {
    at: "2026-09-28T00:00:00.000Z",
    local: "2026-09-28T09:00+09:00",
    zone: "Asia/Tokyo",
    offset_minutes: 540,
    note: "",
  },
];
const serverResolved = {
  profile: {
    profile_id: "6c1d3e4f-2222-4333-8444-555566667777",
    name: "Review helper",
    revision: 1,
    provider: "claude",
    model: null,
    permission_mode: "default",
    mode_source: "settings",
    connection_pinned: false,
  },
  workspace: {
    workspace_id: "7d2e4f50-3333-4444-8555-666677778888",
    name: "scratch",
    policy: "existing_readonly",
    active: true,
  },
  first_turn: "Review the change and cite concrete evidence.\n\nSummarize overnight news.",
  deadline_minutes: 30,
};
const serverReadiness = { blockers: ["unattended_policy"], run_blockers: [], warnings: [] };

test("list parses the server's body: no paused yet, a claimed run with stop_by, nulls", async () => {
  const claimed = {
    occurrence_id: "8e3f5061-4444-4555-8666-777788889999",
    kind: "schedule",
    scheduled_for: "2026-09-28T00:00:00.000Z",
    trigger_id: null,
    revision: 1,
    state: "claimed",
    reason: null,
    recorded_at: "2026-09-28T00:07:00.250Z",
    attempt: {
      attempt_id: "9f406172-5555-4666-8777-88889999aaaa",
      session_id: "a0517283-6666-4777-8888-9999aaaabbbb",
      state: "claimed",
      outcome: null,
      reason: null,
      profile_id: null,
      profile_revision: null,
      native_version: null,
      created_at: "2026-09-28T00:07:00.250Z",
      dispatched_at: null,
      finished_at: null,
    },
  };
  const row = {
    routine_id: SERVER_ID,
    name: "Daily brief",
    state: "enabled",
    current_revision: 1,
    state_revision: 2,
    schedule: { kind: "daily", timezone: "Asia/Tokyo" },
    next_run: "2026-09-29T00:00:00.000Z",
    last_run: claimed,
    last_attempt: claimed,
    active_run: {
      attempt_id: claimed.attempt.attempt_id,
      session_id: claimed.attempt.session_id,
      state: "claimed",
      stop_by: "2026-09-28T00:52:00.250Z",
    },
  };
  // An older server sends no `last_attempt` (undefined drops the key from the JSON).
  const orphan = {
    ...row,
    routine_id: "b1628394-7777-4888-8999-aaaabbbbcccc",
    last_run: null,
    last_attempt: undefined,
    active_run: { ...row.active_run, stop_by: null },
  };
  const fetchImpl = vi.fn(async () =>
    reply({
      routines: [row, orphan],
      scheduler: { state: "unavailable", attended: true },
    }),
  );
  const result = await createRoutinesApi("tok", fetchImpl).list();
  expect(result.scheduler).toEqual({ state: "unavailable", attended: true, paused: false });
  expect(result.routines[0]?.active_run?.stop_by).toBe("2026-09-28T00:52:00.250Z");
  expect(result.routines[0]?.last_run?.attempt?.outcome).toBeNull();
  expect(result.routines[0]?.last_attempt).toEqual(claimed);
  expect(result.routines[1]?.active_run?.stop_by).toBeNull();
  expect(result.routines[1]?.last_run).toBeNull();
  expect(result.routines[1]?.last_attempt).toBeNull();
  const paused = vi.fn(async () =>
    reply({ routines: [], scheduler: { state: "running", attended: true, paused: true } }),
  );
  expect((await createRoutinesApi("tok", paused).list()).scheduler.paused).toBe(true);
});

test("preview parses the rehearsal body, and nulls for a missing profile and workspace", async () => {
  const body = { next_five: serverSlots, readiness: serverReadiness, resolved: serverResolved };
  const api = createRoutinesApi(
    "tok",
    vi.fn(async () => reply(body)),
  );
  const parsed = await api.preview(document);
  expect(parsed.resolved.profile).toMatchObject({ model: null, mode_source: "settings" });
  expect(parsed.resolved.workspace?.active).toBe(true);
  expect(parsed.readiness).toEqual(serverReadiness);
  const bare = {
    next_five: [],
    readiness: { blockers: ["profile_missing"], run_blockers: ["profile_missing"], warnings: [] },
    resolved: { profile: null, workspace: null, first_turn: null, deadline_minutes: 30 },
  };
  const lost = await createRoutinesApi(
    "tok",
    vi.fn(async () => reply(bare)),
  ).preview(document);
  expect(lost.resolved).toEqual(bare.resolved);
  const unsourced = {
    ...bare,
    resolved: {
      ...serverResolved,
      profile: { ...serverResolved.profile, model: null, permission_mode: null, mode_source: null },
    },
  };
  const none = await createRoutinesApi(
    "tok",
    vi.fn(async () => reply(unsourced)),
  ).preview(document);
  expect(none.resolved.profile).toMatchObject({ permission_mode: null, mode_source: null });
  const pinned = {
    ...serverResolved,
    profile: { ...serverResolved.profile, connection_pinned: true },
  };
  const connected = await createRoutinesApi(
    "tok",
    vi.fn(async () => reply({ ...bare, resolved: pinned })),
  ).preview(document);
  expect(connected.resolved.profile?.connection_pinned).toBe(true);
});

test("preview rejects a resolved profile whose connection_pinned is missing or not a boolean", async () => {
  const { connection_pinned: _drop, ...without } = serverResolved.profile;
  for (const profile of [without, { ...without, connection_pinned: "yes" }]) {
    const body = {
      next_five: serverSlots,
      readiness: serverReadiness,
      resolved: { ...serverResolved, profile },
    };
    await expect(
      createRoutinesApi(
        "tok",
        vi.fn(async () => reply(body)),
      ).preview(document),
    ).rejects.toBeDefined();
  }
});

test("preview keeps a Bot run's system prompt and rejects one that is not text", async () => {
  const preview = (system_prompt: unknown) =>
    createRoutinesApi(
      "tok",
      vi.fn(async () =>
        reply({
          next_five: serverSlots,
          readiness: serverReadiness,
          resolved: { ...serverResolved, first_turn: "Summarize.", system_prompt },
        }),
      ),
    ).preview(document);
  expect((await preview("Be brief.")).resolved.system_prompt).toBe("Be brief.");
  expect((await preview(null)).resolved.system_prompt).toBeNull();
  for (const bad of [1, ["Be brief."]]) await expect(preview(bad)).rejects.toBeDefined();
});

test("get parses the detail body: item fields plus document, readiness and resolved", async () => {
  const body = {
    routine_id: SERVER_ID,
    name: "Daily brief",
    state: "draft",
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: "daily", timezone: "Asia/Tokyo" },
    next_run: null,
    last_run: null,
    active_run: null,
    document: { ...document, name: "Daily brief" },
    tested_revision: null,
    activates_at: "2026-09-27T00:00:00.000Z",
    next_five: serverSlots,
    readiness: serverReadiness,
    resolved: serverResolved,
  };
  const parsed = await createRoutinesApi(
    "tok",
    vi.fn(async () => reply(body)),
  ).get(SERVER_ID);
  expect(parsed).toMatchObject({
    routine_id: SERVER_ID,
    tested_revision: null,
    activates_at: "2026-09-27T00:00:00.000Z",
    readiness: serverReadiness,
    resolved: serverResolved,
  });
  expect(parsed.next_five[0]?.note).toBeNull();
});

test("runs sends limit and an optional before", async () => {
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL) => reply({ runs: [run] }));
  const api = createRoutinesApi("tok", fetchImpl);
  expect(await api.runs(ID, { limit: 20 })).toHaveLength(1);
  await api.runs(ID, { limit: 20, before: "o1" });
  expect(fetchImpl.mock.calls[0]?.[0]).toBe(`/api/v2/routines/${ID}/runs?limit=20`);
  expect(fetchImpl.mock.calls[1]?.[0]).toBe(`/api/v2/routines/${ID}/runs?limit=20&before=o1`);
});

test("writes carry a JSON body with the closed key set of each route", async () => {
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
    reply(change, 201),
  );
  const api = createRoutinesApi("tok", fetchImpl);
  await api.create(COMMAND, document);
  await api.revise(ID, COMMAND, 2, document);
  await api.setState(ID, "pause", COMMAND, 3);
  fetchImpl.mockResolvedValueOnce(
    reply({ occurrence_id: "o", attempt_id: "a", session_id: "s" }, 202),
  );
  expect(await api.run(ID, COMMAND, 2)).toMatchObject({ session_id: "s" });
  const calls = fetchImpl.mock.calls.map(([url, init]) => [
    url,
    init?.method,
    JSON.parse(String(init?.body)),
    new Headers(init?.headers).get("content-type"),
  ]);
  expect(calls).toEqual([
    ["/api/v2/routines", "POST", { command_id: COMMAND, document }, "application/json"],
    [
      `/api/v2/routines/${ID}`,
      "PATCH",
      { command_id: COMMAND, expected_revision: 2, document },
      "application/json",
    ],
    [
      `/api/v2/routines/${ID}/pause`,
      "POST",
      { command_id: COMMAND, expected_state_revision: 3 },
      "application/json",
    ],
    [
      `/api/v2/routines/${ID}/run`,
      "POST",
      { command_id: COMMAND, expected_revision: 2 },
      "application/json",
    ],
  ]);
});

test("preview posts only the document", async () => {
  const fetchImpl = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => reply(preview));
  await createRoutinesApi("tok", fetchImpl).preview(document);
  const [url, init] = fetchImpl.mock.calls[0] ?? [];
  expect(url).toBe("/api/v2/routines/preview");
  expect(JSON.parse(String(init?.body))).toEqual({ document });
});

test("a 422 keeps its blockers and never the server's message", async () => {
  const fetchImpl = vi.fn(async () =>
    reply(
      { error: { code: "not_ready", message: "secret text", blockers: ["missing_profile"] } },
      422,
    ),
  );
  const error = await createRoutinesApi("tok", fetchImpl)
    .setState(ID, "enable", COMMAND, 1)
    .catch((caught) => caught);
  expect(error).toMatchObject({ status: 422, code: "not_ready", blockers: ["missing_profile"] });
  expect(error.message).toBe("not_ready");
});

test("blockers come only from a 422", async () => {
  const fetchImpl = vi.fn(async () => reply({ error: { code: "conflict", blockers: ["x"] } }, 409));
  await expect(createRoutinesApi("tok", fetchImpl).run(ID, COMMAND, 1)).rejects.toMatchObject({
    status: 409,
    code: "conflict",
    blockers: [],
  });
});

test("a non-JSON failure body falls back to unavailable with its status", async () => {
  const fetchImpl = vi.fn(async () => new Response("<html>", { status: 502 }));
  await expect(createRoutinesApi("tok", fetchImpl).list()).rejects.toMatchObject({
    status: 502,
    code: "unavailable",
  });
});

test("a transport failure is unavailable", async () => {
  const fetchImpl = vi.fn(async () => {
    throw new TypeError("network");
  });
  await expect(createRoutinesApi("tok", fetchImpl).list()).rejects.toMatchObject({
    status: 0,
    code: "unavailable",
  });
});

test("a caller abort is cancelled, before and during the request", async () => {
  const before = new AbortController();
  before.abort();
  const fetchImpl = vi.fn(
    (_url: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) =>
        init?.signal?.addEventListener("abort", () => reject(new DOMException("a", "AbortError"))),
      ),
  );
  const api = createRoutinesApi("tok", fetchImpl);
  await expect(api.list(before.signal)).rejects.toMatchObject({ status: 0, code: "cancelled" });
  expect(fetchImpl).not.toHaveBeenCalled();
  const during = new AbortController();
  const pending = api.list(during.signal);
  during.abort();
  await expect(pending).rejects.toMatchObject({ status: 0, code: "cancelled" });
});

test("the 15 s timeout is intake_timeout", async () => {
  vi.useFakeTimers();
  const fetchImpl = vi.fn(
    (_url: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) =>
        init?.signal?.addEventListener("abort", () => reject(new DOMException("a", "AbortError"))),
      ),
  );
  const pending = createRoutinesApi("tok", fetchImpl)
    .list()
    .catch((caught) => caught);
  await vi.advanceTimersByTimeAsync(15_000);
  expect(await pending).toMatchObject({ status: 408, code: "intake_timeout" });
});

test("a response missing a field the UI reads is invalid_response", async () => {
  const { name: _name, ...nameless } = item;
  const fetchImpl = vi.fn(async () =>
    reply({ routines: [nameless], scheduler: { state: "running", attended: true, paused: false } }),
  );
  await expect(createRoutinesApi("tok", fetchImpl).list()).rejects.toMatchObject({
    status: 0,
    code: "invalid_response",
  });
  const badAttempt = vi.fn(async () =>
    reply({
      routines: [{ ...item, last_attempt: { ...run, recorded_at: null } }],
      scheduler: { state: "running", attended: true, paused: false },
    }),
  );
  await expect(createRoutinesApi("tok", badAttempt).list()).rejects.toMatchObject({
    code: "invalid_response",
  });
  const noReadiness = vi.fn(async () => reply({ ...detail, readiness: { blockers: [] } }));
  await expect(createRoutinesApi("tok", noReadiness).get(ID)).rejects.toMatchObject({
    code: "invalid_response",
  });
});

test("a body over 2 MiB is refused", async () => {
  const fetchImpl = vi.fn(async () => new Response("x".repeat((2 << 20) + 1), { status: 200 }));
  await expect(createRoutinesApi("tok", fetchImpl).list()).rejects.toMatchObject({
    code: "invalid_response",
  });
});
