import { afterEach, describe, expect, it, vi } from "vitest";
import {
  capabilitiesFixture,
  exportPreviewFixture,
  FAKE_PREVIEW_ID,
  FAKE_VAULT_ROOT,
  FAKE_WORKSPACE_ID,
  importPreviewFixture,
  inspectionFixture,
  listingFixture,
  locationsFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import {
  createShellApi,
  type ExportInclude,
  type ResetCategory,
  type SettingsView,
  type ShellApi,
  ShellApiError,
} from "./shellApi";

const TOKEN = "launch-token-9f2ad41c";
const COMMAND_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const AT = "2026-09-17T00:00:00.000Z";
const DESTINATION = "D:\\Exports\\viki-diagnostics.json";

interface Recorded {
  path: string;
  method: string;
  headers: Record<string, string>;
  body: string | undefined;
}

/** A Response stand-in: jsdom has no fetch, and the client only ever reads these four members. */
function answer(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function recorder(respond: (path: string) => Response | Promise<Response>): {
  calls: Recorded[];
  api: ShellApi;
} {
  const calls: Recorded[] = [];
  const fetchImpl = (async (input: unknown, init: RequestInit = {}) => {
    const path = String(input);
    calls.push({
      path,
      method: String(init.method),
      headers: { ...(init.headers as Record<string, string>) },
      body: init.body === undefined ? undefined : String(init.body),
    });
    return respond(path);
  }) as unknown as typeof fetch;
  return { calls, api: createShellApi(TOKEN, { fetch: fetchImpl }) };
}

interface RouteCase {
  name: string;
  call: (api: ShellApi) => Promise<unknown>;
  method: string;
  path: string;
  body: unknown;
  response: unknown;
  status: number;
  /** One required member replaced by a number, so the shape check must refuse the answer. */
  broken: unknown;
}

const CHECK_ANSWER = {
  check: "git",
  status: "ok",
  result: { version: "2.45.0" },
  checked_at: AT,
  duration_ms: 10,
};
const PATCH_ANSWER = { ...settingsFixture(), command_id: COMMAND_ID, replayed: false };
const SELECT_ANSWER = {
  selected_id: FAKE_WORKSPACE_ID,
  active_id: FAKE_WORKSPACE_ID,
  restart_required: false,
};
const SUGGESTION_ANSWER = { parent: "D:\\VIKI\\Vaults", folder: "My notes", path: FAKE_VAULT_ROOT };
const EXPORT_ANSWER = {
  path: DESTINATION,
  bytes: 512,
  sha256: "1".repeat(64),
  command_id: COMMAND_ID,
  replayed: false,
};
const RESET_ANSWER = {
  scope_of: FAKE_WORKSPACE_ID,
  results: { drafts: { status: "done", count: 2 } },
};

function mutate(response: unknown, key: string): unknown {
  return { ...(response as Record<string, unknown>), [key]: 42 };
}

const ROUTES: RouteCase[] = [
  {
    name: "capabilities",
    call: (api) => api.capabilities(),
    method: "GET",
    path: "/api/v2/capabilities",
    body: undefined,
    response: capabilitiesFixture(),
    status: 200,
    broken: mutate(capabilitiesFixture(), "provider"),
  },
  {
    name: "diagnostics",
    call: (api) => api.diagnostics(),
    method: "GET",
    path: "/api/v2/diagnostics",
    body: undefined,
    response: { app: { mode: "normal" } },
    status: 200,
    broken: 42,
  },
  {
    name: "runCheck",
    call: (api) => api.runCheck(COMMAND_ID, "git"),
    method: "POST",
    path: "/api/v2/diagnostics/checks",
    body: { command_id: COMMAND_ID, check: "git" },
    response: CHECK_ANSWER,
    status: 200,
    broken: mutate(CHECK_ANSWER, "status"),
  },
  {
    name: "settings",
    call: (api) => api.settings(),
    method: "GET",
    path: "/api/v2/settings",
    body: undefined,
    response: settingsFixture(),
    status: 200,
    broken: mutate(settingsFixture(), "keys"),
  },
  {
    name: "patchSettings",
    call: (api) => api.patchSettings(COMMAND_ID, 3, { "appearance.density": "compact" }),
    method: "PATCH",
    path: "/api/v2/settings",
    body: {
      command_id: COMMAND_ID,
      expected_revision: 3,
      changes: { "appearance.density": "compact" },
    },
    response: PATCH_ANSWER,
    status: 200,
    broken: mutate(PATCH_ANSWER, "replayed"),
  },
  {
    name: "workspaces",
    call: (api) => api.workspaces(),
    method: "GET",
    path: "/api/v2/workspaces",
    body: undefined,
    response: listingFixture([workspaceFixture()]),
    status: 200,
    broken: mutate(listingFixture([workspaceFixture()]), "active_id"),
  },
  {
    name: "selectWorkspace",
    call: (api) => api.selectWorkspace(COMMAND_ID, FAKE_WORKSPACE_ID),
    method: "POST",
    path: "/api/v2/workspaces/select",
    body: { command_id: COMMAND_ID, workspace_id: FAKE_WORKSPACE_ID },
    response: SELECT_ANSWER,
    status: 200,
    broken: mutate(SELECT_ANSWER, "restart_required"),
  },
  {
    name: "vaultSuggestion",
    call: (api) => api.vaultSuggestion("My notes"),
    method: "GET",
    path: "/api/v2/vaults/suggestion?name=My%20notes",
    body: undefined,
    response: SUGGESTION_ANSWER,
    status: 200,
    broken: mutate(SUGGESTION_ANSWER, "folder"),
  },
  {
    name: "folderPickerStatus",
    call: (api) => api.folderPickerStatus(),
    method: "GET",
    path: "/api/v2/folders/pick",
    body: undefined,
    response: { available: true, multiple: false, reason: null },
    status: 200,
    broken: mutate({ available: true, multiple: false, reason: null }, "available"),
  },
  {
    name: "pickFolders",
    call: (api) => api.pickFolders(true, "Choose a folder"),
    method: "POST",
    path: "/api/v2/folders/pick",
    body: { multiple: true, title: "Choose a folder" },
    response: { paths: [FAKE_VAULT_ROOT] },
    status: 200,
    broken: { paths: [42] },
  },
  {
    name: "createVault",
    call: (api) => api.createVault(COMMAND_ID, { name: "My notes" }),
    method: "POST",
    path: "/api/v2/vaults/create",
    body: { command_id: COMMAND_ID, name: "My notes" },
    response: { workspace: workspaceFixture(), created_files: ["Welcome.md"] },
    status: 201,
    broken: { workspace: workspaceFixture(), created_files: 42 },
  },
  {
    name: "createVault with template",
    call: (api) => api.createVault(COMMAND_ID, { name: "My notes", template: "viki" }),
    method: "POST",
    path: "/api/v2/vaults/create",
    body: { command_id: COMMAND_ID, name: "My notes", template: "viki" },
    response: { workspace: workspaceFixture(), created_files: ["Welcome.md"] },
    status: 201,
    broken: { workspace: workspaceFixture(), created_files: 42 },
  },
  {
    name: "openVault",
    call: (api) => api.openVault(COMMAND_ID, FAKE_VAULT_ROOT),
    method: "POST",
    path: "/api/v2/vaults/open",
    body: { command_id: COMMAND_ID, path: FAKE_VAULT_ROOT },
    response: { workspace: workspaceFixture(), inspection: inspectionFixture() },
    status: 201,
    broken: { workspace: workspaceFixture(), inspection: 42 },
  },
  {
    name: "openCodeFolder",
    call: (api) => api.openCodeFolder(COMMAND_ID, FAKE_VAULT_ROOT),
    method: "POST",
    path: "/api/v2/workspaces/code",
    body: { command_id: COMMAND_ID, path: FAKE_VAULT_ROOT },
    response: { workspace: workspaceFixture(), inspection: inspectionFixture() },
    status: 201,
    broken: { workspace: workspaceFixture(), inspection: 42 },
  },
  {
    name: "cancelBootstrap",
    call: (api) => api.cancelBootstrap(COMMAND_ID, FAKE_WORKSPACE_ID),
    method: "POST",
    path: `/api/v2/vaults/${FAKE_WORKSPACE_ID}/cancel-bootstrap`,
    body: { command_id: COMMAND_ID },
    response: { workspace_id: FAKE_WORKSPACE_ID, cancelled: true },
    status: 200,
    broken: { workspace_id: FAKE_WORKSPACE_ID, cancelled: 42 },
  },
  {
    name: "removeWorkspace",
    call: (api) => api.removeWorkspace(COMMAND_ID, FAKE_WORKSPACE_ID),
    method: "POST",
    path: `/api/v2/workspaces/${FAKE_WORKSPACE_ID}/remove`,
    body: { command_id: COMMAND_ID },
    response: { workspace_id: FAKE_WORKSPACE_ID, removed: true },
    status: 200,
    broken: { workspace_id: FAKE_WORKSPACE_ID, removed: 42 },
  },
  {
    name: "policy",
    call: (api) => api.policy(FAKE_WORKSPACE_ID),
    method: "GET",
    path: `/api/v2/vaults/${FAKE_WORKSPACE_ID}/policy`,
    body: undefined,
    response: inspectionFixture(),
    status: 200,
    broken: mutate(inspectionFixture(), "markers"),
  },
  {
    name: "setPolicy",
    call: (api) =>
      api.setPolicy(COMMAND_ID, FAKE_WORKSPACE_ID, 1, "ordinary", ["instructions_present"]),
    method: "PATCH",
    path: `/api/v2/vaults/${FAKE_WORKSPACE_ID}/policy`,
    body: {
      command_id: COMMAND_ID,
      expected_policy_revision: 1,
      policy: "ordinary",
      acknowledged: ["instructions_present"],
    },
    response: { policy: "ordinary", policy_revision: 2, revision: 2, note_write: false },
    status: 200,
    broken: { policy: "ordinary", policy_revision: 2, revision: 2, note_write: 42 },
  },
  {
    name: "dataLocations",
    call: (api) => api.dataLocations(),
    method: "GET",
    path: "/api/v2/data/locations",
    body: undefined,
    response: locationsFixture(),
    status: 200,
    broken: mutate(locationsFixture(), "categories"),
  },
  {
    name: "exportPreview",
    call: (api) => api.exportPreview("diagnostics", { paths: true }),
    method: "POST",
    path: "/api/v2/exports/preview",
    body: { kind: "diagnostics", include: { paths: true } },
    response: exportPreviewFixture(),
    status: 200,
    broken: mutate(exportPreviewFixture(), "files"),
  },
  {
    name: "exportWrite",
    call: (api) => api.exportWrite(COMMAND_ID, FAKE_PREVIEW_ID, DESTINATION),
    method: "POST",
    path: "/api/v2/exports",
    body: { command_id: COMMAND_ID, preview_id: FAKE_PREVIEW_ID, destination: DESTINATION },
    response: EXPORT_ANSWER,
    status: 201,
    broken: mutate(EXPORT_ANSWER, "sha256"),
  },
  {
    name: "importPreview",
    call: (api) => api.importPreview('{"format_version":1}'),
    method: "POST",
    path: "/api/v2/imports/preview",
    body: { kind: "settings", content: '{"format_version":1}' },
    response: importPreviewFixture(),
    status: 200,
    broken: mutate(importPreviewFixture(), "changes"),
  },
  {
    name: "importApply",
    call: (api) => api.importApply(FAKE_PREVIEW_ID, COMMAND_ID, 3, ["appearance.density"]),
    method: "POST",
    path: `/api/v2/imports/${FAKE_PREVIEW_ID}/apply`,
    body: { command_id: COMMAND_ID, expected_revision: 3, keys: ["appearance.density"] },
    response: PATCH_ANSWER,
    status: 200,
    broken: mutate(PATCH_ANSWER, "command_id"),
  },
  {
    name: "reset",
    call: (api) => api.reset(COMMAND_ID, ["drafts"]),
    method: "POST",
    path: "/api/v2/data/reset",
    body: { command_id: COMMAND_ID, categories: ["drafts"], confirmation: "RESET" },
    response: RESET_ANSWER,
    status: 200,
    broken: mutate(RESET_ANSWER, "results"),
  },
];

/** Reject with the error the client produced, never a test-authored one. */
async function refusal(promise: Promise<unknown>): Promise<ShellApiError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof ShellApiError) return error;
    throw error;
  }
  throw new Error("expected a ShellApiError");
}

function errorAnswer(status: number, error: Record<string, unknown>): Response {
  return answer({ error }, status);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("shellApi", () => {
  it("each call sends exactly its request and nothing identifying", async () => {
    for (const route of ROUTES) {
      const { calls, api } = recorder(() => answer(route.response, route.status));
      await route.call(api);
      expect(calls).toHaveLength(1);
      const sent = calls[0];
      if (sent === undefined) throw new Error("no request recorded");
      expect([route.name, sent.path]).toEqual([route.name, route.path]);
      expect([route.name, sent.method]).toEqual([route.name, route.method]);
      expect(Object.keys(sent.headers).sort()).toEqual(
        route.body === undefined ? ["authorization"] : ["authorization", "content-type"],
      );
      expect(sent.headers.authorization).toBe(`Bearer ${TOKEN}`);
      expect(sent.body === undefined ? undefined : JSON.parse(sent.body)).toEqual(route.body);
      // Hard rule 7: the token authenticates and does nothing else.
      expect(sent.path).not.toContain(TOKEN);
      expect(sent.body ?? "").not.toContain(TOKEN);
    }
  });

  it("a wrong shape is refused without throwing server text", async () => {
    for (const route of ROUTES) {
      const { api } = recorder(() => answer(route.broken, route.status));
      const error = await refusal(route.call(api));
      expect([route.name, error.code, error.status]).toEqual([route.name, "invalid_response", 0]);
      expect(error.message).toBe("The shell operation could not be completed.");
    }
  });

  it.each([undefined, null, true, -1, 1.5, "3", Number.MAX_SAFE_INTEGER + 1])(
    "an import preview without a valid settings revision (%s) is refused",
    async (revision) => {
      const preview: Record<string, unknown> = {
        ...importPreviewFixture(),
        settings_revision: revision,
      };
      if (revision === undefined) delete preview.settings_revision;
      const { api } = recorder(() => answer(preview));
      expect((await refusal(api.importPreview("{}"))).code).toBe("invalid_response");
    },
  );

  it("a 2xx status the routes never use is refused", async () => {
    const { api } = recorder(() => answer(capabilitiesFixture(), 202));
    expect((await refusal(api.capabilities())).code).toBe("invalid_response");
  });

  it("create and open report 201 as created and 200 as an existing registration", async () => {
    const created = recorder(() =>
      answer({ workspace: workspaceFixture(), created_files: [] }, 201),
    );
    expect((await created.api.createVault(COMMAND_ID, { name: "n" })).created).toBe(true);
    const existing = recorder(() =>
      answer({ workspace: workspaceFixture(), inspection: inspectionFixture() }, 200),
    );
    expect((await existing.api.openVault(COMMAND_ID, FAKE_VAULT_ROOT)).created).toBe(false);
  });

  it("cancel reports what it cancelled", async () => {
    const { api } = recorder(() =>
      answer({ workspace_id: FAKE_WORKSPACE_ID, cancelled: true }, 200),
    );
    expect(await api.cancelBootstrap(COMMAND_ID, FAKE_WORKSPACE_ID)).toEqual({
      workspace_id: FAKE_WORKSPACE_ID,
      cancelled: true,
    });
  });

  it("a cancel that names another workspace is refused", async () => {
    const { api } = recorder(() => answer({ workspace_id: FAKE_PREVIEW_ID, cancelled: true }, 200));
    expect((await refusal(api.cancelBootstrap(COMMAND_ID, FAKE_WORKSPACE_ID))).code).toBe(
      "invalid_response",
    );
  });

  it("remove reports what it removed", async () => {
    const { api } = recorder(() => answer({ workspace_id: FAKE_WORKSPACE_ID, removed: true }, 200));
    expect(await api.removeWorkspace(COMMAND_ID, FAKE_WORKSPACE_ID)).toEqual({
      workspace_id: FAKE_WORKSPACE_ID,
      removed: true,
    });
  });

  it("a remove that names another workspace is refused", async () => {
    const { api } = recorder(() => answer({ workspace_id: FAKE_PREVIEW_ID, removed: true }, 200));
    expect((await refusal(api.removeWorkspace(COMMAND_ID, FAKE_WORKSPACE_ID))).code).toBe(
      "invalid_response",
    );
  });

  it("an empty picker result means cancelled, not a refusal", async () => {
    const { api } = recorder(() => answer({ paths: [] }, 200));
    expect(await api.pickFolders(false, "Pick a folder")).toEqual([]);
  });

  it("setPolicy carries through an ordinary vault's true note_write", async () => {
    const { api } = recorder(() =>
      answer({ policy: "ordinary", policy_revision: 2, revision: 2, note_write: true }, 200),
    );
    const result = await api.setPolicy(COMMAND_ID, FAKE_WORKSPACE_ID, 1, "ordinary", []);
    expect(result.note_write).toBe(true);
  });

  it("root_physical_id is accepted valid or absent, refused non-digit or over 80 chars", async () => {
    const valid = recorder(() =>
      answer(listingFixture([workspaceFixture({ root_physical_id: "1:12345" })]), 200),
    );
    expect((await valid.api.workspaces()).workspaces[0]?.root_physical_id).toBe("1:12345");

    const absent = recorder(() => answer(listingFixture([workspaceFixture()]), 200));
    await expect(absent.api.workspaces()).resolves.toBeDefined();

    const nonDigit = recorder(() =>
      answer(listingFixture([workspaceFixture({ root_physical_id: "x:1" })]), 200),
    );
    expect((await refusal(nonDigit.api.workspaces())).code).toBe("invalid_response");

    const overLong = recorder(() =>
      answer(listingFixture([workspaceFixture({ root_physical_id: `${"1".repeat(80)}:1` })]), 200),
    );
    expect((await refusal(overLong.api.workspaces())).code).toBe("invalid_response");
  });

  it("a pending bootstrap row with no root_physical_id key still lists", async () => {
    const { api } = recorder(() =>
      answer(listingFixture([workspaceFixture({ bootstrap: "pending" })]), 200),
    );
    const listing = await api.workspaces();
    expect(listing.workspaces).toHaveLength(1);
    expect(listing.workspaces[0]?.bootstrap).toBe("pending");
    expect(listing.workspaces[0]?.root_physical_id).toBeUndefined();
  });

  it("an assets item with a bad bytes value is refused", async () => {
    const locations = locationsFixture();
    const assetsCategory = locations.categories.find((category) => category.id === "assets");
    if (assetsCategory === undefined || assetsCategory.id !== "assets") {
      throw new Error("no assets category");
    }
    assetsCategory.items = [
      { path: "D:\\AppData\\viki-agent\\blobs\\x.png", bytes: -1, partial: false },
    ];
    const { api } = recorder(() => answer(locations, 200));
    expect((await refusal(api.dataLocations())).code).toBe("invalid_response");
  });

  it("a stale revision hands back the current document", async () => {
    const current: SettingsView = settingsFixture({ "appearance.density": "compact" });
    const { api } = recorder(() =>
      errorAnswer(409, {
        code: "stale_revision",
        message: "Another window changed these settings.",
        current_revision: 9,
        current,
      }),
    );
    const error = await refusal(api.patchSettings(COMMAND_ID, 3, { "appearance.theme": "dark" }));
    expect([error.status, error.code]).toEqual([409, "stale_revision"]);
    expect(error.currentRevision).toBe(9);
    expect(error.current?.effective["appearance.density"]).toBe("compact");
    // The server's own sentence never becomes the error's own text.
    expect(error.message).toBe("The shell operation could not be completed.");
    expect(error.extra).toEqual({});
  });

  it("a conflict whose document is unreadable is still a conflict", async () => {
    const { api } = recorder(() =>
      errorAnswer(409, { code: "stale_revision", current_revision: 9, current: { version: "x" } }),
    );
    const error = await refusal(api.patchSettings(COMMAND_ID, 3, {}));
    expect([error.code, error.currentRevision, error.current]).toEqual(["stale_revision", 9, null]);
  });

  it("extra members are reachable by key only", async () => {
    const collision = recorder(() =>
      errorAnswer(409, { code: "case_collision", display: "D:\\VIKI\\vaults\\my notes" }),
    );
    const collided = await refusal(collision.api.openVault(COMMAND_ID, FAKE_VAULT_ROOT));
    expect(collided.extra.display).toBe("D:\\VIKI\\vaults\\my notes");
    expect(Object.isFrozen(collided.extra)).toBe(true);

    const stalled = recorder(() =>
      errorAnswer(409, {
        code: "destination_not_empty",
        hint: "cancel_bootstrap",
        workspace_id: FAKE_WORKSPACE_ID,
      }),
    );
    const stalledError = await refusal(stalled.api.createVault(COMMAND_ID, { name: "My notes" }));
    expect(stalledError.hint).toBe("cancel_bootstrap");
    expect(stalledError.extra.workspace_id).toBe(FAKE_WORKSPACE_ID);
    // `hint` is a typed member, so it is not repeated in the bag the views read by key.
    expect("hint" in stalledError.extra).toBe(false);
    expect("code" in stalledError.extra).toBe(false);
    expect("message" in stalledError.extra).toBe(false);

    const unacknowledged = recorder(() =>
      errorAnswer(409, { code: "acknowledgement_required", required: ["hooks_present"] }),
    );
    const missing = await refusal(
      unacknowledged.api.setPolicy(COMMAND_ID, FAKE_WORKSPACE_ID, 1, "ordinary", []),
    );
    expect(missing.extra.required).toEqual(["hooks_present"]);

    const tooLarge = recorder(() =>
      errorAnswer(413, { code: "settings_too_large", current_bytes: 17_000 }),
    );
    const oversize = await refusal(tooLarge.api.patchSettings(COMMAND_ID, 3, {}));
    expect([oversize.status, oversize.extra.current_bytes]).toEqual([413, 17_000]);

    // A policy conflict reports its own revision in `extra`, not in `currentRevision`: that one
    // belongs to the settings document, and the two must not be confused for each other.
    const stalePolicy = recorder(() =>
      errorAnswer(409, { code: "stale_revision", current_policy_revision: 4 }),
    );
    const staleError = await refusal(
      stalePolicy.api.setPolicy(COMMAND_ID, FAKE_WORKSPACE_ID, 1, "ordinary", []),
    );
    expect(staleError.extra.current_policy_revision).toBe(4);
    expect([staleError.current, staleError.currentRevision]).toEqual([null, null]);

    const volume = recorder(() =>
      errorAnswer(422, { code: "unsupported_location", reason: "cloud_sync" }),
    );
    const refused = await refusal(volume.api.openVault(COMMAND_ID, FAKE_VAULT_ROOT));
    expect([refused.status, refused.code, refused.extra.reason]).toEqual([
      422,
      "unsupported_location",
      "cloud_sync",
    ]);
  });

  it("a body the client cannot read is never quoted back", async () => {
    const unreadable = {
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError("Unexpected token < in D:\\secret\\answer");
      },
    } as unknown as Response;
    const ok = recorder(() => unreadable);
    const okError = await refusal(ok.api.capabilities());
    expect([okError.status, okError.code]).toEqual([0, "invalid_response"]);
    expect(okError.message).toBe("The shell operation could not be completed.");

    const refused = recorder(() => ({ ...unreadable, ok: false, status: 503 }) as Response);
    const refusedError = await refusal(refused.api.settings());
    expect([refusedError.status, refusedError.code]).toEqual([503, "http_error"]);
    expect(refusedError.message).not.toContain("secret");
    expect(refusedError.extra).toEqual({});
  });

  it("a suggestion refuses on the query budget the server really applies", async () => {
    // 114 CJK characters: legal for createVault (256 characters) but "name=" plus nine encoded
    // ASCII characters each is 1,031 bytes, past the server's 1,024-byte query-string bound.
    const long = "あ".repeat(114);
    expect(long.length).toBeLessThanOrEqual(256);
    expect(encodeURIComponent(long).length + 5).toBeGreaterThan(1024);

    const { calls, api } = recorder(() => answer(SUGGESTION_ANSWER));
    const error = await refusal(api.vaultSuggestion(long));
    expect([error.status, error.code]).toEqual([0, "invalid_input"]);
    expect(calls).toHaveLength(0);

    // The same name is a request, not a refusal, on the route that bounds characters.
    const creating = recorder(() =>
      answer({ workspace: workspaceFixture(), created_files: [] }, 201),
    );
    await creating.api.createVault(COMMAND_ID, { name: long });
    expect(creating.calls).toHaveLength(1);

    // One character shorter fits, so the bound is the encoded length and not the name length.
    const shorter = "あ".repeat(113);
    const fitting = recorder(() => answer(SUGGESTION_ANSWER));
    await fitting.api.vaultSuggestion(shorter);
    expect(fitting.calls[0]?.path).toBe(
      `/api/v2/vaults/suggestion?name=${encodeURIComponent(shorter)}`,
    );
  });

  it("per-key codes ride the error, not the row's own text", async () => {
    const { api } = recorder(() =>
      errorAnswer(400, {
        code: "invalid_settings",
        message: "Check the settings and try again.",
        fields: { "identity.assistant_name": "too_long" },
      }),
    );
    const error = await refusal(
      api.patchSettings(COMMAND_ID, 3, { "identity.assistant_name": "" }),
    );
    expect(error.fields).toEqual({ "identity.assistant_name": "too_long" });
    expect(Object.isFrozen(error.fields)).toBe(true);
  });

  it("an unrecognisable code becomes http_error", async () => {
    const shouting = recorder(() => errorAnswer(500, { code: "INTERNAL FAILURE" }));
    expect((await refusal(shouting.api.settings())).code).toBe("http_error");
    const bodyless = recorder(() => answer("not json at all", 503));
    expect((await refusal(bodyless.api.settings())).code).toBe("http_error");
  });

  it("a transport failure is network_error, never the exception", async () => {
    const fetchImpl = (async () => {
      throw new TypeError("Failed to fetch D:\\secret");
    }) as unknown as typeof fetch;
    const api = createShellApi(TOKEN, { fetch: fetchImpl });
    const error = await refusal(api.capabilities());
    expect([error.status, error.code]).toEqual([0, "network_error"]);
    expect(error.message).not.toContain("secret");
  });

  it("a call that never settles aborts on its own clock", async () => {
    vi.useFakeTimers();
    const never = (async () => new Promise<Response>(() => {})) as unknown as typeof fetch;
    const api = createShellApi(TOKEN, { fetch: never });

    const capabilities = refusal(api.capabilities());
    await vi.advanceTimersByTimeAsync(14_999);
    let settled = false;
    void capabilities.then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect((await capabilities).code).toBe("timeout");

    const check = refusal(api.runCheck(COMMAND_ID, "git"));
    await vi.advanceTimersByTimeAsync(19_999);
    let checkSettled = false;
    void check.then(() => {
      checkSettled = true;
    });
    await Promise.resolve();
    expect(checkSettled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect((await check).code).toBe("timeout");

    const locations = refusal(api.dataLocations());
    await vi.advanceTimersByTimeAsync(29_999);
    let locationsSettled = false;
    void locations.then(() => {
      locationsSettled = true;
    });
    await Promise.resolve();
    expect(locationsSettled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect((await locations).code).toBe("timeout");

    const pick = refusal(api.pickFolders(false, "Pick a folder"));
    await vi.advanceTimersByTimeAsync(899_999);
    let pickSettled = false;
    void pick.then(() => {
      pickSettled = true;
    });
    await Promise.resolve();
    expect(pickSettled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect((await pick).code).toBe("timeout");
  });

  it("refuses its own caller before a bad request reaches the server", async () => {
    const { calls, api } = recorder(() => answer(settingsFixture()));
    const cases: Promise<unknown>[] = [
      api.patchSettings("not-a-uuid", 3, {}),
      api.patchSettings(COMMAND_ID, -1, {}),
      api.createVault(COMMAND_ID, { name: "" }),
      api.createVault(COMMAND_ID, { name: "n", parent: "" }),
      api.createVault(COMMAND_ID, { name: "n", template: "other" as unknown as "blank" }),
      api.openVault(COMMAND_ID, "x".repeat(4097)),
      api.selectWorkspace(COMMAND_ID, "not-a-uuid"),
      api.setPolicy(COMMAND_ID, FAKE_WORKSPACE_ID, 0, "ordinary", []),
      api.setPolicy(COMMAND_ID, FAKE_WORKSPACE_ID, 1, "ordinary", ["x".repeat(65)]),
      api.importPreview("x".repeat(262_145)),
      api.exportPreview("diagnostics", { unknown: true } as unknown as ExportInclude),
      api.reset(COMMAND_ID, ["drafts", "drafts"]),
      api.reset(COMMAND_ID, []),
      api.reset(COMMAND_ID, ["registrations"] as unknown as ResetCategory[]),
      api.runCheck(COMMAND_ID, "vault" as unknown as "git"),
      api.vaultSuggestion("n".repeat(257)),
      api.pickFolders(true, ""),
      api.pickFolders(true, "x".repeat(121)),
      api.removeWorkspace(COMMAND_ID, "not-a-uuid"),
    ];
    for (const attempt of cases) {
      const error = await refusal(attempt);
      expect([error.status, error.code]).toEqual([0, "invalid_input"]);
    }
    expect(calls).toHaveLength(0);
  });

  it("an empty suggestion name is a request, not a refusal", async () => {
    const { calls, api } = recorder(() => answer(SUGGESTION_ANSWER));
    await api.vaultSuggestion("");
    expect(calls[0]?.path).toBe("/api/v2/vaults/suggestion?name=");
  });

  it("approvals is an accepted reset category", async () => {
    const response = {
      scope_of: FAKE_WORKSPACE_ID,
      results: { approvals: { status: "partial", count: 127, kept: 12 } },
    };
    const { calls, api } = recorder(() => answer(response));
    const result = await api.reset(COMMAND_ID, ["approvals"]);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body === undefined ? undefined : JSON.parse(calls[0].body)).toEqual({
      command_id: COMMAND_ID,
      categories: ["approvals"],
      confirmation: "RESET",
    });
    expect(result.results.approvals).toEqual({ status: "partial", count: 127, kept: 12 });
  });

  it("provider readiness parses source, version_status, and tested fields", async () => {
    const { api } = recorder(() =>
      answer(
        capabilitiesFixture({
          provider: {
            source: "installed",
            version_status: "tested",
            tested: ["2.1.281", "2.1.282"],
          },
        }),
        200,
      ),
    );
    const capabilities = await api.capabilities();
    expect(capabilities.provider.claude.source).toBe("installed");
    expect(capabilities.provider.claude.version_status).toBe("tested");
    expect(capabilities.provider.claude.tested).toEqual(["2.1.281", "2.1.282"]);
  });

  it("provider readiness rejects unknown version_status", async () => {
    const { api } = recorder(() =>
      answer(
        capabilitiesFixture({
          provider: {
            source: "installed",
            version_status: "unknown_value" as unknown as "unknown" | "tested" | "older" | "newer",
            tested: ["2.1.281", "2.1.282"],
          },
        }),
        200,
      ),
    );
    expect((await refusal(api.capabilities())).code).toBe("invalid_response");
  });

  it("provider readiness rejects missing tested", async () => {
    const { api } = recorder(() => {
      const caps = capabilitiesFixture({
        provider: {
          source: "installed",
          version_status: "tested",
        },
      });
      const provider = caps.provider.claude as unknown as Record<string, unknown>;
      delete provider.tested;
      return answer(caps, 200);
    });
    expect((await refusal(api.capabilities())).code).toBe("invalid_response");
  });

  it("provider readiness rejects 3-item tested", async () => {
    const { api } = recorder(() =>
      answer(
        capabilitiesFixture({
          provider: {
            source: "installed",
            version_status: "tested",
            tested: ["2.1.281", "2.1.282", "2.1.283"] as unknown as [string, string],
          },
        }),
        200,
      ),
    );
    expect((await refusal(api.capabilities())).code).toBe("invalid_response");
  });
});
