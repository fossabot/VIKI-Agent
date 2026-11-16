import { afterEach, describe, expect, it, vi } from "vitest";
import { type BotRow, BotsApiError, createBotsApi } from "./botsApi";

const BOT = "00000000-0000-4000-8000-000000000001";
const PROFILE = "00000000-0000-4000-8000-000000000002";
const WORKSPACE = "00000000-0000-4000-8000-000000000003";
const SESSION = "00000000-0000-4000-8000-000000000004";
const OTHER = "00000000-0000-6000-c000-000000000005";
const STAMP = "2026-10-14T00:00:00.000Z";
const scheduler = { state: "running", attended: true, paused: false };
const bot: BotRow = {
  bot_id: BOT,
  profile_id: PROFILE,
  workspace_id: WORKSPACE,
  chat_session_id: SESSION,
  state_revision: 1,
  created_at: STAMP,
  updated_at: STAMP,
  profile: {
    revision: 2,
    archived: false,
    identity: {
      name: "Planner",
      purpose: "Review my tasks",
      avatar_asset_id: null,
      accent: "blue",
      shape: "circle",
    },
  },
  workspace: { name: "Workspace", policy: "ordinary", active: true },
  chat: { profile_revision: 1, started: true, archived: false },
  routine_ids: [OTHER],
  delivery: { enabled: false, available: true, reason: "off" },
  latest_delivery: {
    attempt_id: OTHER,
    routine_id: OTHER,
    session_id: OTHER,
    outcome: "succeeded",
    attention: "notify",
    reason: "runs_to_review",
    state: "off",
    delivery_reason: "off",
    created_at: STAMP,
    updated_at: STAMP,
  },
};
const prepared = {
  launchable: true,
  fields: [],
  blockers: [],
  warnings: [],
  prefs: { mode: "default", effort: "high", model: "sonnet" },
  session_id: SESSION,
  profile_id: PROFILE,
  profile_revision: 1,
  generation: OTHER,
  workspace_id: WORKSPACE,
  workspace_revision: 1,
  policy: "ordinary",
  policy_revision: 1,
  settings_revision: 0,
  runtime: "claude",
  capability_key: "a".repeat(64),
  draft: { key: SESSION, revision: 0, text: "", saved_at: null, attachments: [] },
};
const ok = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const listing = (row: unknown = bot) => ({ bots: [row], scheduler });

afterEach(() => vi.useRealTimers());

describe("Bots API", () => {
  it("reads bounded thread metadata with authentication and validates its owner", async () => {
    const thread = {
      session_id: SESSION,
      profile_revision: 1,
      created_at: STAMP,
      started: true,
      title: "<b>Task</b>",
      archived: false,
    };
    const body = { bot_id: BOT, threads: [thread], has_more: false };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(body));
    const api = createBotsApi("secret", fetcher);
    expect(await api.threads?.(BOT)).toEqual(body);
    expect(fetcher.mock.calls[0]?.[0]).toBe(`/api/v2/bots/${BOT}/threads`);
    expect(fetcher.mock.calls[0]?.[1]?.headers).toEqual({ authorization: "Bearer secret" });
    const unicode = { ...body, threads: [{ ...thread, title: "😀".repeat(256) }] };
    fetcher.mockResolvedValue(ok(unicode));
    expect(await api.threads?.(BOT)).toEqual(unicode);
    for (const invalid of [
      { ...body, bot_id: OTHER },
      { ...body, threads: [thread, thread] },
      { ...body, threads: Array.from({ length: 51 }, () => thread) },
      { ...body, threads: [{ ...thread, title: "bad\nname" }] },
      { ...body, threads: [{ ...thread, profile_revision: 0 }] },
    ]) {
      fetcher.mockResolvedValue(ok(invalid));
      await expect(api.threads?.(BOT)).rejects.toMatchObject({ code: "invalid_response" });
    }
  });
  it("reads exact bounded roster and detail with Bearer auth", async () => {
    const detail = { bot, chat_status: { state: "reopenable", blockers: [] } };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok(listing()))
      .mockResolvedValueOnce(ok(detail));
    const api = createBotsApi("secret", fetcher);
    expect(await api.list()).toEqual(listing());
    expect(await api.get(BOT)).toEqual(detail);
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual([
      "/api/v2/bots",
      `/api/v2/bots/${BOT}`,
    ]);
    expect(fetcher.mock.calls[0]?.[1]?.headers).toEqual({ authorization: "Bearer secret" });
  });

  it.each(['"', "\0"])("preserves 64 legal JSON-escaped Bot rows %#", async (escaped) => {
    const ids = Array.from(
      { length: 64 },
      (_, index) => `00000000-0000-4000-8000-${(index + 1).toString(16).padStart(12, "0")}`,
    );
    const rows = ids.map((id) => ({
      ...bot,
      bot_id: id,
      profile: {
        ...bot.profile,
        identity: {
          ...bot.profile?.identity,
          name: escaped.repeat(128),
          purpose: escaped.repeat(2048),
        },
      },
      workspace: { ...bot.workspace, name: escaped.repeat(256) },
      routine_ids: ids,
    }));
    const value = { bots: rows, scheduler };
    expect(new TextEncoder().encode(JSON.stringify(value)).byteLength).toBeGreaterThan(256 << 10);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(value));
    const api = createBotsApi("secret", fetcher);
    expect(await api.list()).toEqual(value);
    fetcher.mockResolvedValue(ok({ ...value, bots: [...rows, { ...rows[0], bot_id: OTHER }] }));
    await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("sends exact create, CAS, delete and workspace reset bodies", async () => {
    const changed = { ...bot, state_revision: 2 };
    const removed = { removed: true, bot_id: BOT, paused_routine_ids: [OTHER] };
    const reset = { workspace_id: WORKSPACE, removed_bot_ids: [BOT], paused_routine_ids: [OTHER] };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok({ bot, replayed: false }, 201))
      .mockResolvedValueOnce(ok({ bot: changed }))
      .mockResolvedValueOnce(ok({ bot: changed }))
      .mockResolvedValueOnce(ok(removed))
      .mockResolvedValueOnce(ok(reset));
    const api = createBotsApi("secret", fetcher);
    expect(await api.create(BOT, PROFILE, 2, WORKSPACE)).toEqual({ bot, replayed: false });
    expect(await api.newChat(BOT, 1)).toEqual({ bot: changed });
    expect(await api.setDelivery(BOT, 1, true)).toEqual({ bot: changed });
    expect(await api.remove(BOT, 1)).toEqual(removed);
    expect(await api.reset(WORKSPACE)).toEqual(reset);
    expect(
      fetcher.mock.calls.map(([path, init]) => [
        path,
        init?.method,
        JSON.parse(String(init?.body)),
      ]),
    ).toEqual([
      [
        "/api/v2/bots",
        "POST",
        { bot_id: BOT, profile_id: PROFILE, expected_profile_revision: 2, workspace_id: WORKSPACE },
      ],
      [`/api/v2/bots/${BOT}/new-chat`, "POST", { expected_state_revision: 1 }],
      [`/api/v2/bots/${BOT}/delivery`, "POST", { expected_state_revision: 1, enabled: true }],
      [`/api/v2/bots/${BOT}`, "DELETE", { expected_state_revision: 1 }],
      ["/api/v2/bots/reset", "POST", { workspace_id: WORKSPACE }],
    ]);
    expect(
      fetcher.mock.calls.every(
        ([, init]) =>
          (init?.headers as Record<string, string> | undefined)?.["content-type"] ===
          "application/json",
      ),
    ).toBe(true);
  });

  it("validates prepared and blocked chat through the shared preview parser", async () => {
    const blocked = {
      launchable: false,
      fields: [],
      blockers: ["runtime.model"],
      profile_id: PROFILE,
      profile_revision: 2,
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok({ bot_id: BOT, state_revision: 1, preview: prepared }))
      .mockResolvedValueOnce(ok({ bot_id: BOT, state_revision: 1, preview: blocked }, 422));
    const api = createBotsApi("secret", fetcher);
    expect((await api.chat(bot)).preview).toEqual(prepared);
    expect((await api.chat(bot)).preview).toEqual(blocked);
    expect(fetcher.mock.calls[0]?.[1]?.body).toBe(JSON.stringify({ expected_state_revision: 1 }));
  });

  it.each([
    { bot_id: OTHER, state_revision: 1, preview: prepared },
    { bot_id: BOT, state_revision: 2, preview: prepared },
    { bot_id: BOT, state_revision: 1, preview: { ...prepared, profile_id: OTHER } },
    { bot_id: BOT, state_revision: 1, preview: { ...prepared, workspace_id: OTHER } },
    {
      bot_id: BOT,
      state_revision: 1,
      preview: { ...prepared, session_id: OTHER, draft: { ...prepared.draft, key: OTHER } },
    },
    {
      bot_id: BOT,
      state_revision: 1,
      preview: {
        ...prepared,
        bot_memory: { bot_id: OTHER, ref: `bot-memory/${OTHER}`, routine: false },
      },
    },
    { bot_id: BOT, state_revision: 1, preview: { ...prepared, generation: null } },
    { bot_id: BOT, state_revision: 1, preview: { ...prepared, extra: true } },
  ])("rejects cross-Bot or malformed chat responses %#", async (body) => {
    await expect(
      createBotsApi("secret", vi.fn<typeof fetch>().mockResolvedValue(ok(body))).chat(bot),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects a blocked preview for another profile and preserves fixed 422 errors", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        ok(
          {
            bot_id: BOT,
            state_revision: 1,
            preview: {
              launchable: false,
              fields: [],
              blockers: ["runtime.model"],
              profile_id: OTHER,
              profile_revision: 1,
            },
          },
          422,
        ),
      )
      .mockResolvedValueOnce(ok({ error: { code: "profile_archived" } }, 422));
    const api = createBotsApi("secret", fetcher);
    await expect(api.chat(bot)).rejects.toMatchObject({ code: "invalid_response" });
    await expect(api.chat(bot)).rejects.toMatchObject({ status: 422, code: "profile_archived" });
  });

  it.each([
    { ...bot, extra: true },
    { ...bot, profile: { ...bot.profile, identity: { ...bot.profile?.identity, accent: "pink" } } },
    {
      ...bot,
      profile: { ...bot.profile, identity: { ...bot.profile?.identity, avatar_asset_id: "bad" } },
    },
    {
      ...bot,
      profile: { ...bot.profile, identity: { ...bot.profile?.identity, name: "é".repeat(65) } },
    },
    { ...bot, profile: { ...bot.profile, identity: { ...bot.profile?.identity, extra: true } } },
    { ...bot, profile_id: PROFILE.toUpperCase().replace("002", "00A") },
    { ...bot, state_revision: 0 },
    { ...bot, state_revision: Number.MAX_SAFE_INTEGER + 1 },
    { ...bot, created_at: "2026-02-30T00:00:00.000Z" },
    { ...bot, chat: { ...bot.chat, profile_revision: 0 } },
    { ...bot, routine_ids: [OTHER, OTHER] },
    { ...bot, routine_ids: Array(65).fill(OTHER) },
    { ...bot, delivery: { ...bot.delivery, enabled: "false" } },
    { ...bot, delivery: { ...bot.delivery, reason: "secret_destination" } },
    { ...bot, workspace: { ...bot.workspace, policy: "unknown" } },
    { ...bot, workspace: { ...bot.workspace, name: "a".repeat(257) } },
    { ...bot, latest_delivery: { ...bot.latest_delivery, outcome: "running" } },
    { ...bot, latest_delivery: { ...bot.latest_delivery, attention: "ask" } },
    { ...bot, latest_delivery: { ...bot.latest_delivery, reason: "unknown" } },
    { ...bot, latest_delivery: { ...bot.latest_delivery, state: "queued" } },
    { ...bot, latest_delivery: { ...bot.latest_delivery, delivery_reason: "https://secret" } },
  ])("rejects malformed Bot rows %#", async (row) => {
    await expect(
      createBotsApi("secret", vi.fn<typeof fetch>().mockResolvedValue(ok(listing(row)))).list(),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("uses UTF-8 identity byte bounds and nullable missing state", async () => {
    const identity = bot.profile?.identity;
    const boundary = {
      ...bot,
      profile: {
        ...bot.profile,
        identity: { ...identity, name: "é".repeat(64), purpose: "é".repeat(1024) },
      },
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok(listing(boundary)))
      .mockResolvedValueOnce(
        ok(
          listing({
            ...boundary,
            profile: {
              ...boundary.profile,
              identity: { ...boundary.profile.identity, purpose: "é".repeat(1025) },
            },
          }),
        ),
      )
      .mockResolvedValueOnce(
        ok(
          listing({
            ...boundary,
            profile: {
              ...boundary.profile,
              identity: { ...boundary.profile.identity, name: "\ud800" },
            },
          }),
        ),
      )
      .mockResolvedValueOnce(
        ok(
          listing({
            ...bot,
            profile: null,
            workspace: null,
            chat: { profile_revision: null, started: false, archived: false },
            latest_delivery: null,
          }),
        ),
      );
    const api = createBotsApi("secret", fetcher);
    expect((await api.list()).bots[0]?.profile?.identity.purpose).toHaveLength(1024);
    await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
    await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
    expect((await api.list()).bots[0]?.profile).toBeNull();
  });

  it.each([
    { bots: [bot, bot], scheduler },
    { bots: Array(65).fill(bot), scheduler },
    { bots: [bot], scheduler: { ...scheduler, state: "starting" } },
    { bots: [bot], scheduler, extra: true },
  ])("rejects duplicate, excessive or malformed roster data %#", async (body) => {
    await expect(
      createBotsApi("secret", vi.fn<typeof fetch>().mockResolvedValue(ok(body))).list(),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects mismatched detail, mutation and receipt identities", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        ok({ bot: { ...bot, bot_id: OTHER }, chat_status: { state: "prepared", blockers: [] } }),
      )
      .mockResolvedValueOnce(ok({ bot: { ...bot, workspace_id: OTHER }, replayed: true }))
      .mockResolvedValueOnce(ok({ bot: { ...bot, bot_id: OTHER } }))
      .mockResolvedValueOnce(ok({ removed: true, bot_id: OTHER, paused_routine_ids: [] }))
      .mockResolvedValueOnce(
        ok({ workspace_id: OTHER, removed_bot_ids: [], paused_routine_ids: [] }),
      );
    const api = createBotsApi("secret", fetcher);
    for (const run of [
      () => api.get(BOT),
      () => api.create(BOT, PROFILE, 2, WORKSPACE),
      () => api.newChat(BOT, 1),
      () => api.remove(BOT, 1),
      () => api.reset(WORKSPACE),
    ])
      await expect(run()).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects unbounded or malformed detail blockers and reset IDs", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        ok({ bot, chat_status: { state: "blocked", blockers: Array(65).fill("profile_changed") } }),
      )
      .mockResolvedValueOnce(ok({ bot, chat_status: { state: "blocked", blockers: ["<unsafe>"] } }))
      .mockResolvedValueOnce(
        ok({ workspace_id: WORKSPACE, removed_bot_ids: [BOT, BOT], paused_routine_ids: [] }),
      );
    const api = createBotsApi("secret", fetcher);
    await expect(api.get(BOT)).rejects.toMatchObject({ code: "invalid_response" });
    await expect(api.get(BOT)).rejects.toMatchObject({ code: "invalid_response" });
    await expect(api.reset(WORKSPACE)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects invalid input before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const api = createBotsApi("secret", fetcher);
    for (const run of [
      () => api.get("../bad"),
      () => api.create(BOT, PROFILE, 0, WORKSPACE),
      () => api.chat({ ...bot, chat_session_id: "bad" }),
      () => api.newChat(BOT, NaN),
      () => api.setDelivery(BOT, 1, "yes" as unknown as boolean),
      () => api.remove(BOT, 0),
      () => api.reset("bad"),
    ])
      await expect(run()).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("bounds raw response bytes separately for roster, single operations and chat", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(" ".repeat((2 << 20) + 1)))
      .mockResolvedValueOnce(new Response(" ".repeat((256 << 10) + 1)))
      .mockResolvedValueOnce(new Response(" ".repeat((256 << 10) + 1)))
      .mockResolvedValueOnce(
        ok({
          bot_id: BOT,
          state_revision: 1,
          preview: { ...prepared, draft: { ...prepared.draft, text: "a".repeat(300_000) } },
        }),
      )
      .mockResolvedValueOnce(new Response(" ".repeat((2 << 20) + 1)));
    const api = createBotsApi("secret", fetcher);
    await expect(api.list()).rejects.toMatchObject({ code: "response_too_large" });
    await expect(api.get(BOT)).rejects.toMatchObject({ code: "response_too_large" });
    await expect(api.newChat(BOT, 1)).rejects.toMatchObject({ code: "response_too_large" });
    expect((await api.chat(bot)).preview).toHaveProperty("draft.text", "a".repeat(300_000));
    await expect(api.chat(bot)).rejects.toMatchObject({ code: "response_too_large" });
  });

  it("rejects invalid JSON or UTF-8 without leaking its body", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response("private"))
      .mockResolvedValueOnce(new Response(new Uint8Array([0xff])));
    const api = createBotsApi("secret", fetcher);
    await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
    await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("pins chosen chat identities before an in-flight request", async () => {
    let resolve!: (value: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockImplementation(
      () =>
        new Promise<Response>((ready) => {
          resolve = ready;
        }),
    );
    const chosen = { ...bot };
    const pending = createBotsApi("secret", fetcher).chat(chosen);
    chosen.chat_session_id = OTHER;
    resolve(ok({ bot_id: BOT, state_revision: 1, preview: prepared }));
    expect((await pending).preview).toHaveProperty("session_id", SESSION);
  });

  it("keeps fixed errors without raw messages or automatic retries", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok({ error: { code: "conflict", message: "private" } }, 409))
      .mockResolvedValueOnce(ok({ error: { code: "unexpected_secret", message: "private" } }, 503))
      .mockRejectedValueOnce(new Error("private"));
    const api = createBotsApi("secret", fetcher);
    await expect(api.setDelivery(BOT, 1, true)).rejects.toMatchObject({
      status: 409,
      code: "conflict",
    });
    await expect(api.list()).rejects.toMatchObject({
      code: "unavailable",
      message: "The Bot operation could not be completed.",
    });
    await expect(api.list()).rejects.toBeInstanceOf(BotsApiError);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("distinguishes caller abort from deadline and cleans its timers", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn<typeof fetch>().mockImplementation(
      (_path, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), {
            once: true,
          });
        }),
    );
    const api = createBotsApi("secret", fetcher);
    const controller = new AbortController();
    const caller = expect(api.list(controller.signal)).rejects.toMatchObject({ code: "cancelled" });
    controller.abort();
    await caller;
    const deadline = expect(api.list()).rejects.toMatchObject({
      status: 408,
      code: "intake_timeout",
    });
    await vi.advanceTimersByTimeAsync(15_000);
    await deadline;
    expect(vi.getTimerCount()).toBe(0);
    const preAborted = new AbortController();
    preAborted.abort();
    await expect(api.list(preAborted.signal)).rejects.toMatchObject({ code: "cancelled" });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  describe("code folder grant", () => {
    const FOLDER = "00000000-0000-4000-8000-000000000006";
    const granted = {
      ...bot,
      state_revision: 2,
      code_folder: { workspace_id: FOLDER, name: "App" },
    };

    it("accepts an absent, null or named code_folder and keeps the row untouched", async () => {
      const api = (row: unknown) =>
        createBotsApi("secret", vi.fn<typeof fetch>().mockResolvedValue(ok(listing(row))));
      for (const row of [bot, { ...bot, code_folder: null }, granted])
        expect(await api(row).list()).toEqual(listing(row));
    });

    it.each([
      { ...bot, code_folder: {} },
      { ...bot, code_folder: "App" },
      { ...bot, code_folder: [FOLDER] },
      { ...bot, code_folder: { workspace_id: FOLDER } },
      { ...bot, code_folder: { workspace_id: FOLDER, name: "App", extra: true } },
      { ...bot, code_folder: { workspace_id: "bad", name: "App" } },
      { ...bot, code_folder: { workspace_id: FOLDER, name: "" } },
      { ...bot, code_folder: { workspace_id: FOLDER, name: 7 } },
      { ...bot, code_folder: { workspace_id: FOLDER, name: "a".repeat(257) } },
      { ...bot, code_folder: { workspace_id: FOLDER, name: "\ud800" } },
    ])("rejects a malformed code_folder %#", async (row) => {
      await expect(
        createBotsApi("secret", vi.fn<typeof fetch>().mockResolvedValue(ok(listing(row)))).list(),
      ).rejects.toMatchObject({ code: "invalid_response" });
    });

    it("sends the exact CAS body to the grant route and clears with null", async () => {
      const cleared = { ...bot, state_revision: 3, code_folder: null };
      const fetcher = vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(ok({ bot: granted }))
        .mockResolvedValueOnce(ok({ bot: cleared }));
      const api = createBotsApi("secret", fetcher);
      expect(await api.setCodeFolder?.(BOT, 1, FOLDER)).toEqual({ bot: granted });
      expect(await api.setCodeFolder?.(BOT, 2, null)).toEqual({ bot: cleared });
      expect(
        fetcher.mock.calls.map(([path, init]) => [
          path,
          init?.method,
          JSON.parse(String(init?.body)),
          (init?.headers as Record<string, string> | undefined)?.authorization,
        ]),
      ).toEqual([
        [
          `/api/v2/bots/${BOT}/code-folder`,
          "POST",
          { expected_state_revision: 1, workspace_id: FOLDER },
          "Bearer secret",
        ],
        [
          `/api/v2/bots/${BOT}/code-folder`,
          "POST",
          { expected_state_revision: 2, workspace_id: null },
          "Bearer secret",
        ],
      ]);
    });

    it.each([
      [{ bot: { ...granted, bot_id: OTHER } }],
      [{ bot: { ...granted, code_folder: { workspace_id: OTHER, name: "App" } } }],
      [{ bot: { ...granted, code_folder: null } }],
      [{ bot: { ...granted, state_revision: 1 } }],
      [{ bot: { ...bot, state_revision: 2 } }],
      [{ bot: granted, extra: true }],
      [{ bot: { ...granted, extra: true } }],
    ])("rejects a mismatched grant answer %#", async (body) => {
      await expect(
        createBotsApi("secret", vi.fn<typeof fetch>().mockResolvedValue(ok(body))).setCodeFolder?.(
          BOT,
          1,
          FOLDER,
        ),
      ).rejects.toMatchObject({ code: "invalid_response" });
    });

    it("rejects a grant that comes back for a clear request", async () => {
      await expect(
        createBotsApi(
          "secret",
          vi.fn<typeof fetch>().mockResolvedValue(ok({ bot: granted })),
        ).setCodeFolder?.(BOT, 1, null),
      ).rejects.toMatchObject({ code: "invalid_response" });
    });

    it("rejects invalid input before fetch", async () => {
      const fetcher = vi.fn<typeof fetch>();
      const api = createBotsApi("secret", fetcher);
      for (const run of [
        () => api.setCodeFolder?.("../bad", 1, FOLDER),
        () => api.setCodeFolder?.(BOT, 0, FOLDER),
        () => api.setCodeFolder?.(BOT, 1, "bad"),
        () => api.setCodeFolder?.(BOT, 1, undefined as unknown as null),
      ])
        await expect(run()).rejects.toMatchObject({ code: "invalid_input" });
      expect(fetcher).not.toHaveBeenCalled();
    });

    it.each([
      [422, "code_folder_needs_ordinary"],
      [422, "code_folder_untrusted"],
      [422, "code_folder_missing"],
      [422, "code_folder_not_repository"],
      [422, "code_folder_no_commits"],
      [503, "code_folder_git_unavailable"],
      [409, "conflict"],
      [409, "git_settings_changed"],
    ])("keeps the fixed refusal %i %s", async (status, code) => {
      await expect(
        createBotsApi(
          "secret",
          vi
            .fn<typeof fetch>()
            .mockResolvedValue(ok({ error: { code, message: "private" } }, status)),
        ).setCodeFolder?.(BOT, 1, FOLDER),
      ).rejects.toMatchObject({ status, code });
    });
  });
});
