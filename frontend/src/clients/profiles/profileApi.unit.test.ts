import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ACCENTS,
  blankProfileDocument,
  createProfileApi,
  type ProfileDocumentV1,
  type ProfileSummary,
} from "./profileApi";

const PROFILE = "00000000-0000-4000-8000-000000000001";
const TARGET = "00000000-0000-4000-8000-000000000002";
const COMMAND = "00000000-0000-4000-8000-000000000003";
const NOW = "2026-09-21T00:00:00.000Z";

const document = (): ProfileDocumentV1 => ({
  format_version: 1,
  identity: {
    name: "Review helper",
    purpose: "Reviews a selected change",
    avatar_asset_id: null,
    accent: "orange",
    shape: "rounded",
  },
  runtime: { provider: "claude", model: null, effort: "high", permission_mode: null },
  instructions: "Review the change.",
  workspace: { allowed_workspace_ids: [] },
  context: { items: [] },
  native_assets: [],
  tools: { required: [], denied: [], mcp_configuration: null },
  memory: { mode: "provider_inherited", sources: [] },
  limits: {
    timeout_seconds: null,
    max_root_tasks: null,
    token_budget: null,
    currency_budget: null,
  },
  provenance: { kind: "user", derived_from: null, native_source: null },
});

const summary = (profile_id = PROFILE): ProfileSummary => ({
  profile_id,
  revision: 1,
  current_revision: 1,
  state_revision: 1,
  created_at: NOW,
  updated_at: NOW,
  archived: false,
  identity: document().identity,
  runtime: document().runtime,
  provenance: document().provenance,
});

const definition = (revision = 1, current_revision = revision) => ({
  profile_id: PROFILE,
  revision,
  current_revision,
  state_revision: 1,
  created_at: NOW,
  updated_at: NOW,
  archived: false,
  document: document(),
});

const ok = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

afterEach(() => vi.useRealTimers());

describe("accent list", () => {
  it("pins the six accents in order", () => {
    expect(ACCENTS).toEqual(["blue", "green", "orange", "purple", "red", "slate"]);
  });
});

describe("profile API", () => {
  it("imports MCP JSON with bearer auth without saving a profile", async () => {
    const configuration = {
      servers: [
        {
          name: "echo",
          transport: "stdio",
          command: "example-mcp",
          args: [],
          url: null,
          note: null,
        },
      ],
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(configuration));
    const api = createProfileApi("token", fetcher);
    const source = '{"mcpServers":{"echo":{"command":"example-mcp"}}}';
    expect(await api.importMcpConfiguration?.(source)).toEqual(configuration);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(
      "/api/v2/profiles/mcp/import",
      expect.objectContaining({
        method: "POST",
        headers: { authorization: "Bearer token", "content-type": "application/json" },
        body: JSON.stringify({ json: source }),
      }),
    );
  });

  it("bounds MCP import input and rejects malformed import responses", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok({ servers: [{}] }));
    const api = createProfileApi("token", fetcher);
    for (const source of ["", "x".repeat((64 << 10) + 1)]) {
      await expect(api.importMcpConfiguration?.(source)).rejects.toMatchObject({
        code: "invalid_input",
      });
    }
    expect(fetcher).not.toHaveBeenCalled();
    await expect(api.importMcpConfiguration?.("{}")).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  it("lists bounded summaries with bearer authorization", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok({ profiles: [summary()] }));
    const rows = await createProfileApi("token", fetcher).list(false);
    expect(rows[0]?.identity.name).toBe("Review helper");
    expect(fetcher).toHaveBeenCalledWith(
      "/api/v2/profiles?archived=false",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ authorization: "Bearer token" }),
      }),
    );
  });

  it("gets current and named definitions with document-owned display fields", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok(definition(2)))
      .mockResolvedValueOnce(ok(definition(1, 2)));
    const api = createProfileApi("token", fetcher);
    expect((await api.get(PROFILE)).document.identity.name).toBe("Review helper");
    expect((await api.get(PROFILE, 1)).revision).toBe(1);
    expect(fetcher.mock.calls[1]?.[0]).toBe(`/api/v2/profiles/${PROFILE}?revision=1`);
  });

  it("sends exact mutation and comparison contracts", async () => {
    const responses = [
      { profile_id: PROFILE, revision: 1, state_revision: 1 },
      { profile_id: PROFILE, revision: 2, state_revision: 1 },
      { profile_id: PROFILE, revision: 2, state_revision: 2 },
      { profile_id: TARGET, revision: 1, state_revision: 1 },
      {
        profile_id: PROFILE,
        left: 1,
        right: 2,
        changes: [{ field: "identity.name", before: "A", after: "B" }],
      },
    ];
    const fetcher = vi.fn<typeof fetch>();
    for (const response of responses) fetcher.mockResolvedValueOnce(ok(response));
    const api = createProfileApi("token", fetcher);
    await api.create(PROFILE, COMMAND, document());
    await api.revise(PROFILE, COMMAND, 1, document());
    await api.archive(PROFILE, COMMAND, 1, true);
    await api.duplicate(PROFILE, COMMAND, 1, TARGET, "claude");
    await api.compare(PROFILE, 1, 2);

    expect(fetcher.mock.calls.map((call) => [call[0], (call[1] as RequestInit).method])).toEqual([
      ["/api/v2/profiles", "POST"],
      [`/api/v2/profiles/${PROFILE}`, "PATCH"],
      [`/api/v2/profiles/${PROFILE}/archive`, "POST"],
      [`/api/v2/profiles/${PROFILE}/duplicate`, "POST"],
      [`/api/v2/profiles/${PROFILE}/compare?left=1&right=2`, "GET"],
    ]);
    const duplicateRequest = fetcher.mock.calls[3];
    if (!duplicateRequest) throw new Error("missing duplicate request");
    expect(JSON.parse(String((duplicateRequest[1] as RequestInit).body))).toEqual({
      command_id: COMMAND,
      expected_revision: 1,
      target_profile_id: TARGET,
      target_runtime: "claude",
    });
  });

  it.each([
    [
      "too many rows",
      {
        profiles: Array.from({ length: 65 }, (_, index) =>
          summary(`00000000-0000-4000-8000-${String(index).padStart(12, "0")}`),
        ),
      },
    ],
    ["duplicate IDs", { profiles: [summary(), summary()] }],
    ["empty name", { profiles: [{ ...summary(), identity: { ...summary().identity, name: "" } }] }],
    ["wrong current revision", { profiles: [{ ...summary(), current_revision: 2 }] }],
  ])("rejects malformed list responses: %s", async (_case, body) => {
    const api = createProfileApi("token", vi.fn<typeof fetch>().mockResolvedValue(ok(body)));
    await expect(api.list(false)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects mismatched definitions, receipts, and comparisons", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok({ ...definition(), profile_id: TARGET }))
      .mockResolvedValueOnce(ok({ profile_id: PROFILE, revision: 3, state_revision: 1 }))
      .mockResolvedValueOnce(ok({ profile_id: TARGET, left: 1, right: 2, changes: [] }));
    const api = createProfileApi("token", fetcher);
    await expect(api.get(PROFILE)).rejects.toMatchObject({ code: "invalid_response" });
    await expect(api.revise(PROFILE, COMMAND, 1, document())).rejects.toMatchObject({
      code: "invalid_response",
    });
    await expect(api.compare(PROFILE, 1, 2)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects invalid inputs before fetch", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const api = createProfileApi("token", fetcher);
    await expect(api.get("NOT-A-UUID")).rejects.toMatchObject({ code: "invalid_input" });
    await expect(api.create(PROFILE, "bad", document())).rejects.toMatchObject({
      code: "invalid_input",
    });
    await expect(
      api.create(PROFILE, COMMAND, {
        ...document(),
        format_version: 2,
      } as unknown as ProfileDocumentV1),
    ).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("preserves allowed server codes and hides unknown codes and messages", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok({ error: { code: "conflict", message: "private" } }, 409))
      .mockResolvedValueOnce(ok({ error: { code: "invented", message: "private" } }, 500));
    const api = createProfileApi("token", fetcher);
    await expect(api.list(false)).rejects.toMatchObject({ code: "conflict" });
    await expect(api.list(false)).rejects.toMatchObject({ code: "unavailable" });
  });

  it("preserves the avatar_missing code from a 400 refusal", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok({ error: { code: "avatar_missing", message: "private" } }, 400));
    const api = createProfileApi("token", fetcher);
    await expect(api.list(false)).rejects.toMatchObject({ code: "avatar_missing" });
  });

  it("bounds response bytes", async () => {
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new Uint8Array((2 << 20) + 1));
        controller.close();
      },
    });
    const api = createProfileApi(
      "token",
      vi.fn<typeof fetch>().mockResolvedValue(new Response(stream)),
    );
    await expect(api.list(false)).rejects.toMatchObject({ code: "response_too_large" });
  });

  it("distinguishes caller cancellation from the bounded timeout", async () => {
    const cancelled = new AbortController();
    cancelled.abort();
    const fetcher = vi.fn<typeof fetch>();
    await expect(
      createProfileApi("token", fetcher).list(false, cancelled.signal),
    ).rejects.toMatchObject({
      code: "cancelled",
    });
    expect(fetcher).not.toHaveBeenCalled();

    vi.useFakeTimers();
    const pending = vi.fn<typeof fetch>(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          const signal = init?.signal;
          if (!signal) throw new Error("missing signal");
          signal.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    const result = createProfileApi("token", pending).list(false);
    const rejected = expect(result).rejects.toMatchObject({ code: "intake_timeout" });
    await vi.advanceTimersByTimeAsync(15_000);
    await rejected;
  });
});

describe("server error details", () => {
  it("carries field and reason from a 400 invalid_document refusal", async () => {
    const body = {
      error: {
        code: "invalid_document",
        field: "tools.mcp_configuration.servers[0].args[0]",
        reason: "forbidden_text",
      },
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(ok(body, 400));
    const api = createProfileApi("token", fetcher);
    await expect(api.list(false)).rejects.toMatchObject({
      code: "invalid_document",
      field: "tools.mcp_configuration.servers[0].args[0]",
      reason: "forbidden_text",
    });
  });

  it("keeps field and reason null for a 409 conflict and an invalid_document without them", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(ok({ error: { code: "conflict", message: "private" } }, 409))
      .mockResolvedValueOnce(ok({ error: { code: "invalid_document" } }, 400));
    const api = createProfileApi("token", fetcher);
    await expect(api.list(false)).rejects.toMatchObject({
      code: "conflict",
      field: null,
      reason: null,
    });
    await expect(api.list(false)).rejects.toMatchObject({
      code: "invalid_document",
      field: null,
      reason: null,
    });
  });
});

describe("blank profile document", () => {
  it("returns equal but independent objects", () => {
    const first = blankProfileDocument();
    const second = blankProfileDocument();
    expect(first).toEqual(second);
    expect(first).not.toBe(second);
    first.identity.name = "Changed";
    first.context.items.push({ key: "value" });
    expect(second.identity.name).toBe("New profile");
    expect(second.context.items).toEqual([]);
  });
});
