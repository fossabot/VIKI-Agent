import { describe, expect, it, vi } from "vitest";
import { ACCENTS } from "./profileApi";
import {
  botMemoryFolder,
  createProfileLaunchApi,
  ProfileLaunchApiError,
  parseProfilePreview,
} from "./profileLaunchApi";

const PROFILE = "00000000-0000-4000-8000-000000000001";
const COMMAND = "00000000-0000-4000-8000-000000000002";
const SESSION = "00000000-0000-4000-8000-000000000003";
const WORKSPACE = "00000000-0000-4000-8000-000000000004";
const HASH = "a".repeat(64);
const ok = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const common = {
  provider: "claude",
  adapter_version: "profile-v1",
  native_version: "2.1.259",
  observed_at: "2026-09-21T00:00:00.000Z",
  catalog_key: HASH,
};
const fields = [
  {
    field: "runtime.model",
    preview_class: "effective",
    status: "supported",
    effective_value: "sonnet",
    source: "inherited",
    reason: null,
  },
];
const prepared = {
  launchable: true,
  fields,
  blockers: [],
  warnings: [],
  prefs: { mode: "default", effort: "high", model: "sonnet" },
  session_id: SESSION,
  profile_id: PROFILE,
  profile_revision: 1,
  generation: "00000000-0000-4000-8000-000000000005",
  workspace_id: WORKSPACE,
  workspace_revision: 1,
  policy: "ordinary",
  policy_revision: 1,
  settings_revision: 0,
  runtime: "claude",
  capability_key: HASH,
  draft: { key: SESSION, revision: 1, text: "Review", saved_at: null, attachments: [] },
};

describe("profile launch API", () => {
  it("binds a Bot UUID and accepts only its exact memory ref", async () => {
    const memory = { bot_id: COMMAND, ref: `bot-memory/${COMMAND}`, routine: false };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(ok({ ...prepared, bot_memory: memory }));
    const api = createProfileLaunchApi("secret", fetcher);
    const answer = await api.prepare(PROFILE, COMMAND, SESSION, 1, WORKSPACE, undefined, COMMAND);
    expect("bot_memory" in answer && answer.bot_memory).toEqual(memory);
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body)).bot_id).toBe(COMMAND);
    fetcher.mockResolvedValue(ok({ ...prepared, bot_memory: { ...memory, ref: "../other" } }));
    await expect(
      api.prepare(PROFILE, COMMAND, SESSION, 1, WORKSPACE, undefined, COMMAND),
    ).rejects.toMatchObject({ code: "invalid_response" });
    fetcher.mockResolvedValue(
      ok({
        ...prepared,
        bot_memory: { bot_id: SESSION, ref: `bot-memory/${SESSION}`, routine: false },
      }),
    );
    await expect(
      api.prepare(PROFILE, COMMAND, SESSION, 1, WORKSPACE, undefined, COMMAND),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("accepts an app-kept memory path only when it is absolute and ends with the Bot UUID", () => {
    for (const path of [
      `/data/bot-memory/${WORKSPACE}/${COMMAND}`,
      `C:\\data\\bot-memory\\${WORKSPACE}\\${COMMAND}`,
      `c:/data/${COMMAND}`,
    ]) {
      const parsed = parseProfilePreview(
        { ...prepared, bot_memory: { bot_id: COMMAND, path, routine: false } },
        200,
      );
      expect(parsed).toMatchObject({ bot_memory: { bot_id: COMMAND, path, routine: false } });
      expect(botMemoryFolder({ bot_id: COMMAND, path, routine: false })).toBe(path);
    }
    expect(botMemoryFolder({ bot_id: COMMAND, ref: `bot-memory/${COMMAND}`, routine: false })).toBe(
      `bot-memory/${COMMAND}`,
    );
    for (const bad of [
      { bot_id: COMMAND, path: `data/${COMMAND}`, routine: false },
      { bot_id: COMMAND, path: `/data/${SESSION}`, routine: false },
      { bot_id: COMMAND, path: `/data/x${COMMAND}`, routine: false },
      { bot_id: COMMAND, path: `/da\tta/${COMMAND}`, routine: false },
      { bot_id: COMMAND, path: `/da\u0085ta/${COMMAND}`, routine: false },
      { bot_id: COMMAND, path: `/\ud800/${COMMAND}`, routine: false },
      { bot_id: COMMAND, path: `/${"a".repeat(4100)}/${COMMAND}`, routine: false },
      { bot_id: COMMAND, path: "", routine: false },
      { bot_id: COMMAND, path: `/data/${COMMAND}`, ref: `bot-memory/${COMMAND}`, routine: false },
      { bot_id: COMMAND, path: 7, routine: false },
      { bot_id: COMMAND, routine: false },
      { bot_id: COMMAND, path: `/data/${COMMAND}` },
      { bot_id: COMMAND, path: `/data/${COMMAND}`, routine: "no" },
    ])
      expect(parseProfilePreview({ ...prepared, bot_memory: bad }, 200)).toBeNull();
  });
  it("parses an optional code_copy strictly and never invents it", () => {
    const copy = { workspace_id: WORKSPACE, name: "Repo", branch: "viki/bot-20261112-abc123" };
    expect(parseProfilePreview(prepared, 200)).not.toHaveProperty("code_copy");
    for (const good of [copy, { ...copy, branch: null }, null])
      expect(parseProfilePreview({ ...prepared, code_copy: good }, 200)).toEqual({
        ...prepared,
        code_copy: good,
      });
    for (const bad of [
      { ...copy, extra: 1 },
      { workspace_id: WORKSPACE, name: "Repo" },
      { ...copy, workspace_id: "workspace" },
      { ...copy, name: "" },
      { ...copy, name: "x".repeat(513) },
      { ...copy, name: "\ud800" },
      { ...copy, branch: "" },
      { ...copy, branch: 3 },
      {},
      "Repo",
    ])
      expect(parseProfilePreview({ ...prepared, code_copy: bad }, 200)).toBeNull();
  });
  it("shares the existing preview parser without relaxing generation or field checks", () => {
    expect(parseProfilePreview(prepared, 200)).toEqual(prepared);
    expect(parseProfilePreview({ ...prepared, generation: null }, 200)).toBeNull();
    expect(parseProfilePreview({ ...prepared, generation: null }, 200, true)).toEqual({
      ...prepared,
      generation: null,
    });
    expect(parseProfilePreview({ ...prepared, extra: true }, 200)).toBeNull();
    const blocked = {
      launchable: false,
      fields: [],
      blockers: ["runtime.model"],
      profile_id: PROFILE,
      profile_revision: 1,
    };
    expect(parseProfilePreview(blocked, 422)).toEqual(blocked);
    expect(parseProfilePreview(blocked, 200)).toBeNull();
  });
  it("bounds the prepared prefs model at 4096 bytes", () => {
    const at = (model: string) => ({ ...prepared, prefs: { ...prepared.prefs, model } });
    expect(parseProfilePreview(at("m".repeat(4096)), 200)).toEqual(at("m".repeat(4096)));
    expect(parseProfilePreview(at("m".repeat(4097)), 200)).toBeNull();
  });
  it("reads bounded provider capabilities and models with bearer auth", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        ok({
          ...common,
          capabilities: [
            { id: "plan_mode", status: "supported", reason: null, evidence: "fixture_exact" },
          ],
        }),
      )
      .mockResolvedValueOnce(
        ok({
          ...common,
          status: "supported",
          models: [{ value: "sonnet", label: "Sonnet", description: "Observed model" }],
        }),
      );
    const api = createProfileLaunchApi("secret", fetcher);
    expect((await api.capabilities("claude")).capabilities[0]?.status).toBe("supported");
    expect((await api.models("claude")).models[0]?.value).toBe("sonnet");
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([
      "/api/v2/providers/claude/capabilities",
      "/api/v2/providers/claude/models",
    ]);
    expect(fetcher.mock.calls[0]?.[1]?.headers).toEqual({ authorization: "Bearer secret" });
  });

  it("returns a validated prepared draft and sends the exact request", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(prepared));
    const result = await createProfileLaunchApi("secret", fetcher).prepare(
      PROFILE,
      COMMAND,
      SESSION,
      1,
      WORKSPACE,
    );
    expect("session_id" in result && result.session_id).toBe(SESSION);
    expect(fetcher).toHaveBeenCalledWith(
      `/api/v2/profiles/${PROFILE}/start-preview`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          command_id: COMMAND,
          session_id: SESSION,
          expected_profile_revision: 1,
          workspace_id: WORKSPACE,
        }),
      }),
    );
  });

  it("inspects an existing prepared session and permits an offline generation", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      ok({
        ...prepared,
        launchable: false,
        blockers: ["capability_changed"],
        generation: null,
      }),
    );
    const result = await createProfileLaunchApi("secret", fetcher).inspect(SESSION);
    expect(result.generation).toBeNull();
    expect(result.launchable).toBe(false);
    expect(fetcher.mock.calls[0]?.[0]).toBe(`/api/v2/profile-sessions/${SESSION}`);
  });

  it("returns a truthful 422 evaluation without requiring a session", async () => {
    const body = {
      launchable: false,
      fields: [{ ...fields[0], status: "unverified", reason: "model_unverified" }],
      blockers: ["runtime.model"],
      profile_id: PROFILE,
      profile_revision: 1,
    };
    const result = await createProfileLaunchApi(
      "secret",
      vi.fn<typeof fetch>().mockResolvedValue(ok(body, 422)),
    ).prepare(PROFILE, COMMAND, SESSION, 1, WORKSPACE);
    expect(result).toEqual(body);
  });

  it("rejects mismatched identities, malformed fields, and excess provider rows", async () => {
    const mismatched = vi
      .fn<typeof fetch>()
      .mockResolvedValue(ok({ ...prepared, session_id: COMMAND }));
    await expect(
      createProfileLaunchApi("secret", mismatched).prepare(PROFILE, COMMAND, SESSION, 1, WORKSPACE),
    ).rejects.toMatchObject({ code: "invalid_response" });

    const malformed = vi
      .fn<typeof fetch>()
      .mockResolvedValue(ok({ ...prepared, fields: [{ ...fields[0], extra: true }] }));
    await expect(
      createProfileLaunchApi("secret", malformed).prepare(PROFILE, COMMAND, SESSION, 1, WORKSPACE),
    ).rejects.toMatchObject({ code: "invalid_response" });

    const tooMany = vi.fn<typeof fetch>().mockResolvedValue(
      ok({
        ...common,
        capabilities: Array(65).fill({
          id: "x",
          status: "supported",
          reason: null,
          evidence: null,
        }),
      }),
    );
    await expect(
      createProfileLaunchApi("secret", tooMany).capabilities("claude"),
    ).rejects.toMatchObject({ code: "invalid_response" });

    const wrongProvider = vi
      .fn<typeof fetch>()
      .mockResolvedValue(ok({ ...common, provider: "other", capabilities: [] }));
    await expect(
      createProfileLaunchApi("secret", wrongProvider).capabilities("claude"),
    ).rejects.toMatchObject({ code: "invalid_response" });

    const contradictory = vi
      .fn<typeof fetch>()
      .mockResolvedValue(ok({ ...prepared, launchable: false, blockers: [] }));
    await expect(
      createProfileLaunchApi("secret", contradictory).prepare(
        PROFILE,
        COMMAND,
        SESSION,
        1,
        WORKSPACE,
      ),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects invalid input before fetch and preserves allowlisted server errors", async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(
      createProfileLaunchApi("secret", fetcher).capabilities("bad/provider"),
    ).rejects.toBeInstanceOf(ProfileLaunchApiError);
    expect(fetcher).not.toHaveBeenCalled();

    fetcher.mockResolvedValue(ok({ error: { code: "workspace_restart_required" } }, 409));
    await expect(
      createProfileLaunchApi("secret", fetcher).prepare(PROFILE, COMMAND, SESSION, 1, WORKSPACE),
    ).rejects.toMatchObject({ code: "workspace_restart_required" });
  });

  it("distinguishes caller cancellation", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(
      createProfileLaunchApi("secret", vi.fn<typeof fetch>()).prepare(
        PROFILE,
        COMMAND,
        SESSION,
        1,
        WORKSPACE,
        controller.signal,
      ),
    ).rejects.toMatchObject({ code: "cancelled" });
  });

  it("accepts any canonical lower-case UUID, as the server does (str(uuid.UUID(v)) == v)", async () => {
    // Version nibble "6" and variant nibble "c": the server's check never inspects either.
    const sessionId = "00000000-0000-6000-c000-000000000099";
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      ok({
        ...prepared,
        session_id: sessionId,
        draft: { ...prepared.draft, key: sessionId },
      }),
    );
    const result = await createProfileLaunchApi("secret", fetcher).inspect(sessionId);
    expect("session_id" in result && result.session_id).toBe(sessionId);
  });

  it("rejects an upper-case or otherwise non-canonical id", async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(
      createProfileLaunchApi("secret", fetcher).inspect("0000000A-0000-4000-8000-000000000003"),
    ).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("parses an inspected session's identity when the server names one", async () => {
    const identity = {
      name: "Planner",
      avatar_asset_id: null,
      accent: "blue",
      shape: "circle",
      profile_revision: 1,
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok({ ...prepared, identity }));
    const result = await createProfileLaunchApi("secret", fetcher).inspect(SESSION);
    expect(result.identity).toEqual(identity);
  });

  it("parses an inspected session's identity with each of the six accents", async () => {
    for (const accent of ACCENTS) {
      const identity = {
        name: "Planner",
        avatar_asset_id: null,
        accent,
        shape: "circle",
        profile_revision: 1,
      };
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok({ ...prepared, identity }));
      const result = await createProfileLaunchApi("secret", fetcher).inspect(SESSION);
      expect(result.identity).toEqual(identity);
    }
    expect(ACCENTS).toHaveLength(6);
  });

  it("parses an inspected session without an identity", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(prepared));
    const result = await createProfileLaunchApi("secret", fetcher).inspect(SESSION);
    expect(result.identity).toBeUndefined();
  });

  it("rejects a bad accent, an extra identity key, or a mismatched identity revision", async () => {
    const identity = {
      name: "Planner",
      avatar_asset_id: null,
      accent: "blue",
      shape: "circle",
      profile_revision: 1,
    };
    const badAccent = vi
      .fn<typeof fetch>()
      .mockResolvedValue(ok({ ...prepared, identity: { ...identity, accent: "gold" } }));
    await expect(
      createProfileLaunchApi("secret", badAccent).inspect(SESSION),
    ).rejects.toMatchObject({ code: "invalid_response" });

    const extraKey = vi
      .fn<typeof fetch>()
      .mockResolvedValue(ok({ ...prepared, identity: { ...identity, extra: true } }));
    await expect(createProfileLaunchApi("secret", extraKey).inspect(SESSION)).rejects.toMatchObject(
      { code: "invalid_response" },
    );

    const mismatchedRevision = vi
      .fn<typeof fetch>()
      .mockResolvedValue(ok({ ...prepared, identity: { ...identity, profile_revision: 2 } }));
    await expect(
      createProfileLaunchApi("secret", mismatchedRevision).inspect(SESSION),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("parses an inspected session's started flag when present and allows it absent", async () => {
    for (const started of [true, false]) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok({ ...prepared, started }));
      expect((await createProfileLaunchApi("secret", fetcher).inspect(SESSION)).started).toBe(
        started,
      );
    }
    const without = vi.fn<typeof fetch>().mockResolvedValue(ok(prepared));
    expect(
      (await createProfileLaunchApi("secret", without).inspect(SESSION)).started,
    ).toBeUndefined();
  });

  it("rejects a started flag that is not a boolean", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok({ ...prepared, started: 1 }));
    await expect(createProfileLaunchApi("secret", fetcher).inspect(SESSION)).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  it("parses an inspected session's mcp_config when present, absent or null", async () => {
    const mcp = {
      json: '{"mcpServers":{"viki":{"command":"viki-mcp"}}}',
      argv_element: "--mcp-config=C:/data/mcp/profile.json",
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok({ ...prepared, mcp_config: mcp }))
      .mockResolvedValueOnce(ok(prepared))
      .mockResolvedValueOnce(ok({ ...prepared, mcp_config: null }));
    const api = createProfileLaunchApi("secret", fetcher);
    expect((await api.inspect(SESSION)).mcp_config).toEqual(mcp);
    expect((await api.inspect(SESSION)).mcp_config).toBeUndefined();
    expect((await api.inspect(SESSION)).mcp_config).toBeNull();
  });

  it("rejects an inspected mcp_config with missing json, extra key or no prefix", async () => {
    const invalid = [
      { argv_element: "--mcp-config=C:/data/mcp/profile.json" },
      {
        json: "{}",
        argv_element: "--mcp-config=C:/data/mcp/profile.json",
        extra: "x",
      },
      { json: "{}", argv_element: "C:/data/mcp/profile.json" },
    ];
    for (const mcp_config of invalid) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok({ ...prepared, mcp_config }));
      const api = createProfileLaunchApi("secret", fetcher);
      await expect(api.inspect(SESSION)).rejects.toMatchObject({ code: "invalid_response" });
    }
  });

  it("accepts a model value and label up to 4,096 bytes, matching provider_catalogs.py", async () => {
    const long = "a".repeat(4096);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      ok({
        ...common,
        status: "supported",
        models: [{ value: long, label: long, description: "" }],
      }),
    );
    const result = await createProfileLaunchApi("secret", fetcher).models("claude");
    expect(result.models[0]?.value).toBe(long);
  });

  it("rejects a model value or label over 4,096 bytes", async () => {
    const tooLong = "a".repeat(4097);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      ok({
        ...common,
        status: "supported",
        models: [{ value: tooLong, label: "Sonnet", description: "" }],
      }),
    );
    await expect(createProfileLaunchApi("secret", fetcher).models("claude")).rejects.toMatchObject({
      code: "invalid_response",
    });
  });
});
