import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  blankProfileDocument,
  type ProfileApi,
  type ProfileDefinition,
} from "../../clients/profiles/profileApi";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import { useCapabilityLaunch } from "./useCapabilityLaunch";

const PROFILE = "00000000-0000-4000-8000-000000000001";
const WORKSPACE = "00000000-0000-4000-8000-000000000002";
const NOW = "2026-10-12T00:00:00.000Z";

function source(): ProfileDefinition {
  const document = blankProfileDocument();
  document.identity.name = "Synthetic profile";
  document.instructions = "Synthetic instructions";
  document.runtime.model = "synthetic-model";
  document.context.items = [{ kind: "unresolved" }];
  document.native_assets = [{ kind: "skill", relative_id: "example" }];
  document.tools.required = ["Read"];
  document.tools.denied = ["Bash"];
  document.memory.mode = "isolated";
  document.limits.token_budget = 123;
  document.provenance.derived_from = { kind: "example" };
  document.tools.mcp_configuration = {
    servers: [
      {
        name: "Library",
        transport: "stdio",
        command: "synthetic",
        args: ["", "arg"],
        url: null,
        note: "",
      },
      {
        name: "Calendar",
        transport: "http",
        command: null,
        args: [],
        url: "https://example.invalid/mcp",
        note: null,
      },
    ],
  };
  return {
    profile_id: PROFILE,
    revision: 4,
    current_revision: 4,
    state_revision: 1,
    created_at: NOW,
    updated_at: NOW,
    archived: false,
    document,
  };
}

function profileApi(): ProfileApi {
  return {
    list: vi.fn(),
    get: vi.fn(),
    revise: vi.fn(),
    archive: vi.fn(),
    duplicate: vi.fn(),
    compare: vi.fn(),
    create: vi.fn(async (id) => ({ profile_id: id, revision: 1, state_revision: 1 })),
  };
}

function prepared(id: string, profileId = PROFILE, revision = 4): PreparedProfilePreview {
  return {
    launchable: true,
    fields: [],
    blockers: [],
    warnings: [],
    prefs: { mode: "default", effort: "high", model: null },
    session_id: id,
    profile_id: profileId,
    profile_revision: revision,
    generation: PROFILE,
    workspace_id: WORKSPACE,
    workspace_revision: 1,
    policy: "ordinary",
    policy_revision: 1,
    settings_revision: 1,
    runtime: "claude",
    capability_key: "a".repeat(64),
    draft: {
      key: id,
      revision: 1,
      text: "Synthetic instructions",
      saved_at: null,
      attachments: [],
    },
    started: false,
  };
}

function launchApi(): ProfileLaunchApi {
  let preview: PreparedProfilePreview;
  return {
    capabilities: vi.fn(),
    models: vi.fn(),
    prepare: vi.fn(async (profile, _command, id, revision) => {
      preview = prepared(id, profile, revision);
      return preview;
    }),
    inspect: vi.fn(async () => preview),
  };
}

afterEach(cleanup);

it("prepares the exact original revision without creating or revising a profile", async () => {
  const api = profileApi(),
    launch = launchApi(),
    onPrepared = vi.fn();
  const definition = source();
  const { result } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [true, true],
      onPrepared,
    }),
  );
  await act(() => result.current.prepare());
  expect(launch.prepare).toHaveBeenCalledWith(
    PROFILE,
    expect.any(String),
    expect.any(String),
    4,
    WORKSPACE,
    expect.any(AbortSignal),
  );
  expect(api.create).not.toHaveBeenCalled();
  expect(api.revise).not.toHaveBeenCalled();
  expect(launch.inspect).toHaveBeenCalledOnce();
  expect(onPrepared).toHaveBeenCalledWith(expect.any(String), null);
});

it("requires confirmation then copies only the connector subset and preserves every other field", async () => {
  const api = profileApi(),
    launch = launchApi(),
    definition = source();
  const before = structuredClone(definition);
  const { result } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [true, false],
    }),
  );
  await act(() => result.current.prepare());
  expect(api.create).not.toHaveBeenCalled();
  await act(() => result.current.prepare(true));
  const document = vi.mocked(api.create).mock.calls[0]?.[2];
  const originalServers = definition.document.tools.mcp_configuration?.servers as unknown[];
  expect(document).toEqual({
    ...definition.document,
    tools: { ...definition.document.tools, mcp_configuration: { servers: [originalServers[0]] } },
  });
  expect(definition).toEqual(before);
  expect(api.revise).not.toHaveBeenCalled();
  expect(launch.prepare).toHaveBeenCalledWith(
    vi.mocked(api.create).mock.calls[0]?.[0],
    expect.any(String),
    expect.any(String),
    1,
    WORKSPACE,
    expect.any(AbortSignal),
  );
});

it("saves all connectors off as null", async () => {
  const api = profileApi(),
    launch = launchApi(),
    definition = source();
  const { result } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [false, false],
    }),
  );
  await act(() => result.current.prepare(true));
  expect(vi.mocked(api.create).mock.calls[0]?.[2].tools.mcp_configuration).toBeNull();
});

it("keeps identifiers stable on manual identical retries and resets them for changed inclusion", async () => {
  const api = profileApi(),
    launch = launchApi(),
    definition = source();
  vi.mocked(api.create).mockRejectedValue(new Error("Synthetic unavailable"));
  const { result, rerender } = renderHook(
    ({ included }) =>
      useCapabilityLaunch({
        profileApi: api,
        launchApi: launch,
        workspaceId: WORKSPACE,
        source: definition,
        included,
      }),
    { initialProps: { included: [true, false] } },
  );
  await act(() => result.current.prepare(true));
  expect(api.create).toHaveBeenCalledTimes(1);
  await act(() => result.current.prepare(true));
  expect(vi.mocked(api.create).mock.calls[0]?.slice(0, 3)).toEqual(
    vi.mocked(api.create).mock.calls[1]?.slice(0, 3),
  );
  rerender({ included: [false, false] });
  await act(() => result.current.prepare(true));
  expect(vi.mocked(api.create).mock.calls[2]?.[0]).not.toBe(
    vi.mocked(api.create).mock.calls[0]?.[0],
  );
});

it("keeps successful copy and prepare identifiers through an explicit prepare retry", async () => {
  const api = profileApi(),
    launch = launchApi(),
    definition = source();
  vi.mocked(launch.prepare).mockRejectedValueOnce(new ProfileLaunchApiError(409, "conflict"));
  const { result } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [false, true],
    }),
  );
  await act(() => result.current.prepare(true));
  expect(result.current.error).toMatch(/changed/i);
  await act(() => result.current.prepare(true));
  expect(api.create).toHaveBeenCalledOnce();
  expect(vi.mocked(launch.prepare).mock.calls[0]?.slice(0, 5)).toEqual(
    vi.mocked(launch.prepare).mock.calls[1]?.slice(0, 5),
  );
});

it("retains provider blockers and does not hand off blocked or started sessions", async () => {
  const api = profileApi(),
    launch = launchApi(),
    onPrepared = vi.fn();
  vi.mocked(launch.prepare).mockResolvedValue({
    launchable: false,
    fields: [
      {
        field: "tools.required",
        preview_class: "unsupported",
        status: "unsupported",
        effective_value: ["Read"],
        source: "profile",
        reason: "tools_unproven",
      },
    ],
    blockers: ["tools.required"],
    profile_id: PROFILE,
    profile_revision: 4,
  });
  const definition = source();
  const { result } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [true, true],
      onPrepared,
    }),
  );
  await act(() => result.current.prepare());
  expect(result.current.preview?.blockers).toEqual(["tools.required"]);
  expect(launch.inspect).not.toHaveBeenCalled();
  expect(onPrepared).not.toHaveBeenCalled();
  vi.mocked(launch.prepare).mockImplementation(async (profile, _command, id, revision) =>
    prepared(id, profile, revision),
  );
  vi.mocked(launch.inspect).mockImplementation(async (id) => ({ ...prepared(id), started: true }));
  await act(() => result.current.prepare());
  expect(onPrepared).not.toHaveBeenCalled();
  expect(result.current.error).toMatch(/already started/i);
});

it("aborts stale workspace results and gives a changed workspace new identifiers", async () => {
  const api = profileApi(),
    launch = launchApi(),
    onPrepared = vi.fn(),
    definition = source();
  let resolve!: (value: PreparedProfilePreview) => void;
  vi.mocked(launch.prepare).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { result, rerender } = renderHook(
    ({ workspaceId }) =>
      useCapabilityLaunch({
        profileApi: api,
        launchApi: launch,
        workspaceId,
        source: definition,
        included: [true, true],
        onPrepared,
      }),
    { initialProps: { workspaceId: WORKSPACE } },
  );
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.prepare();
  });
  const first = vi.mocked(launch.prepare).mock.calls[0];
  rerender({ workspaceId: PROFILE });
  expect(first?.[5]?.aborted).toBe(true);
  await act(async () => {
    resolve(prepared(first?.[2] ?? ""));
    await pending;
  });
  expect(onPrepared).not.toHaveBeenCalled();
  await act(() => result.current.prepare());
  expect(vi.mocked(launch.prepare).mock.calls[1]?.[2]).not.toBe(first?.[2]);
});

it("blocks duplicate submission and ignores an aborted copy after API replacement", async () => {
  const api = profileApi(),
    next = profileApi(),
    launch = launchApi(),
    definition = source();
  let resolve!: (value: { profile_id: string; revision: number; state_revision: number }) => void;
  vi.mocked(api.create).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { result, rerender } = renderHook(
    ({ profiles }) =>
      useCapabilityLaunch({
        profileApi: profiles,
        launchApi: launch,
        workspaceId: WORKSPACE,
        source: definition,
        included: [true, false],
      }),
    { initialProps: { profiles: api } },
  );
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.prepare(true);
    void result.current.prepare(true);
  });
  expect(api.create).toHaveBeenCalledOnce();
  const first = vi.mocked(api.create).mock.calls[0];
  rerender({ profiles: next });
  expect(first?.[3]?.aborted).toBe(true);
  await act(async () => {
    resolve({ profile_id: first?.[0] ?? "", revision: 1, state_revision: 1 });
    await pending;
  });
  expect(launch.prepare).not.toHaveBeenCalled();
  await act(() => result.current.prepare(true));
  expect(vi.mocked(next.create).mock.calls[0]?.[0]).not.toBe(first?.[0]);
});

it("keeps the prepared session IDs after inspect failure until a manual retry", async () => {
  const api = profileApi(),
    launch = launchApi(),
    definition = source(),
    onPrepared = vi.fn();
  vi.mocked(launch.inspect).mockRejectedValueOnce(new Error("Synthetic inspect unavailable"));
  const { result } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [true, true],
      onPrepared,
    }),
  );
  await act(() => result.current.prepare());
  expect(result.current.ready).toBe(false);
  expect(onPrepared).not.toHaveBeenCalled();
  expect(launch.prepare).toHaveBeenCalledOnce();
  await act(() => result.current.prepare());
  expect(vi.mocked(launch.prepare).mock.calls[1]?.slice(0, 5)).toEqual(
    vi.mocked(launch.prepare).mock.calls[0]?.slice(0, 5),
  );
  expect(onPrepared).toHaveBeenCalledOnce();
});

it("does not hand off a stale profile result after unmount", async () => {
  const api = profileApi(),
    launch = launchApi(),
    definition = source(),
    onPrepared = vi.fn();
  let resolve!: (value: PreparedProfilePreview) => void;
  vi.mocked(launch.inspect).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { result, unmount } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [true, true],
      onPrepared,
    }),
  );
  let pending!: Promise<void>;
  await act(async () => {
    pending = result.current.prepare();
    await Promise.resolve();
  });
  const call = vi.mocked(launch.inspect).mock.calls[0];
  unmount();
  expect(call?.[1]?.aborted).toBe(true);
  await act(async () => {
    resolve(prepared(call?.[0] ?? ""));
    await pending;
  });
  expect(onPrepared).not.toHaveBeenCalled();
});

it("withholds handoff when inspected profile or workspace differs", async () => {
  const api = profileApi(),
    launch = launchApi(),
    definition = source(),
    onPrepared = vi.fn();
  vi.mocked(launch.inspect).mockImplementation(async (id) => ({
    ...prepared(id),
    workspace_id: PROFILE,
  }));
  const { result } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [true, true],
      onPrepared,
    }),
  );
  await act(() => result.current.prepare());
  expect(result.current.ready).toBe(false);
  expect(onPrepared).not.toHaveBeenCalled();
  expect(result.current.error).toBeTruthy();
});

it("requires confirmed never-started state before handing off the server identity", async () => {
  const api = profileApi(),
    launch = launchApi(),
    definition = source(),
    onPrepared = vi.fn();
  vi.mocked(launch.inspect).mockImplementation(async (id) => {
    const preview = prepared(id);
    delete preview.started;
    return preview;
  });
  const { result } = renderHook(() =>
    useCapabilityLaunch({
      profileApi: api,
      launchApi: launch,
      workspaceId: WORKSPACE,
      source: definition,
      included: [true, true],
      onPrepared,
    }),
  );
  await act(() => result.current.prepare());
  expect(onPrepared).not.toHaveBeenCalled();
  const identity = {
    name: "Synthetic identity",
    avatar_asset_id: null,
    accent: "orange" as const,
    shape: "rounded" as const,
    profile_revision: 4,
  };
  vi.mocked(launch.inspect).mockImplementation(async (id) => ({ ...prepared(id), identity }));
  await act(() => result.current.prepare());
  expect(onPrepared).toHaveBeenCalledWith(expect.any(String), identity);
});
