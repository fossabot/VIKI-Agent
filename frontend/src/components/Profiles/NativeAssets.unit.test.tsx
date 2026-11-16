import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type {
  NativeAssetsApi,
  NativeAssetView,
  NativeCatalog,
  NativeSource,
} from "../../clients/profiles/nativeAssetsApi";
import { type ProfileApi, ProfileApiError } from "../../clients/profiles/profileApi";
import { NativeAssets } from "./NativeAssets";

afterEach(cleanup);

const userSource: NativeSource = { source_id: "claude:user", provider: "claude", scope: "user" };
const vaultSource: NativeSource = { source_id: "vault:1", provider: "claude", scope: "workspace" };

function asset(overrides: Partial<NativeAssetView> = {}): NativeAssetView {
  return {
    source_id: "claude:user",
    source_scope: "user",
    provider: "claude",
    relative_id: ".claude/agents/example-agent.md",
    kind: "agent",
    name: "example-agent",
    description: "An example agent.",
    model: null,
    tools: ["Read", "Grep"],
    digest: "a".repeat(64),
    observed_at: "2026-09-28T00:00:00.000Z",
    supported_fields: ["name", "description", "tools"],
    unsupported_fields: [],
    launch_supported: false,
    ...overrides,
  };
}

function catalog(overrides: Partial<NativeCatalog> = {}): NativeCatalog {
  return {
    source_id: "claude:user",
    provider: "claude",
    source_scope: "user",
    assets: [asset()],
    partial: false,
    partial_reasons: [],
    scanned_entries: 1,
    scanned_bytes: 128,
    omitted: 0,
    ...overrides,
  };
}

function fakeApi(overrides: Partial<NativeAssetsApi> = {}): NativeAssetsApi {
  return {
    sources: vi.fn(async () => [userSource]),
    catalog: vi.fn(async () => catalog()),
    ...overrides,
  };
}

function fakeProfileApi(overrides: Partial<ProfileApi> = {}): ProfileApi {
  const unimplemented = () => {
    throw new Error("not used by this test");
  };
  return {
    list: unimplemented,
    get: unimplemented,
    create: vi.fn(async () => ({ profile_id: "p1", revision: 1, state_revision: 1 })),
    revise: unimplemented,
    archive: unimplemented,
    duplicate: unimplemented,
    compare: unimplemented,
    ...overrides,
  };
}

it("loads sources, prefers the workspace catalog, and renders each asset's fields", async () => {
  const catalogFn = vi.fn(async (sourceId: string) =>
    catalog({
      source_id: sourceId,
      assets: [
        asset({ kind: "agent" }),
        asset({
          kind: "skill",
          name: "example-skill",
          relative_id: ".claude/skills/example-skill.md",
        }),
      ],
    }),
  );
  const api = fakeApi({
    sources: vi.fn(async () => [userSource, vaultSource]),
    catalog: catalogFn,
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("example-agent");
  expect(catalogFn).toHaveBeenCalledWith("vault:1", expect.anything());
  expect(screen.getByRole("option", { name: "Your Claude folder" })).not.toBeNull();
  expect(screen.getByRole("option", { name: "This vault" })).not.toBeNull();
  expect(screen.getAllByText("An example agent.")).toHaveLength(2);
  expect(screen.getAllByText(/Read, Grep/)).toHaveLength(2);
  expect(screen.getAllByText(new RegExp("a".repeat(64)))).toHaveLength(2);
  expect(screen.getAllByText("launch unsupported")).toHaveLength(2);
  expect(screen.getByText("example-skill")).not.toBeNull();
  expect(screen.getByRole("heading", { name: "Subagents" })).not.toBeNull();
  expect(screen.getByRole("heading", { name: "Skills" })).not.toBeNull();
  expect(screen.getByRole("button", { name: "All" }).getAttribute("aria-pressed")).toBe("true");
});

it("explains read-only discovery and shows exact source identity with complete read coverage", async () => {
  const api = fakeApi();
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("example-agent", {}, { timeout: 50 });
  const context = within(screen.getByRole("region", { name: "Native catalog context" }));
  expect(context.getByRole("heading", { name: "Read-only native catalog" })).toBeTruthy();
  expect(
    context.getByText("Discovery does not enable an asset or change provider permissions."),
  ).toBeTruthy();
  expect(context.getByText("claude:user")).toBeTruthy();
  expect(context.getByText("Catalog read complete")).toBeTruthy();
  const row = within(screen.getByText("example-agent").closest("li") as HTMLElement);
  expect(row.getByText("Source ID: claude:user").closest("details")).not.toBeNull();
  expect(row.getByText(".claude/agents/example-agent.md")).toBeTruthy();
  expect(api.sources).toHaveBeenCalledTimes(1);
  expect(api.catalog).toHaveBeenCalledTimes(1);
});

it("a partial empty catalog reports limited coverage rather than a complete empty source", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({ assets: [], partial: true, partial_reasons: ["deadline"], omitted: 1 }),
    ),
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  const context = within(
    await screen.findByRole("region", { name: "Native catalog context" }, { timeout: 50 }),
  );
  expect(await context.findByText("Partial catalog", {}, { timeout: 50 })).toBeTruthy();
  expect(context.queryByText("Catalog read complete")).toBeNull();
  expect(screen.getByText("No entries returned in this partial catalog.")).toBeTruthy();
  expect(screen.queryByText("No native assets")).toBeNull();
});

it("a Skills entry seeds the existing filter without creating a profile", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({
        assets: [
          asset(),
          asset({
            kind: "skill",
            name: "example-skill",
            relative_id: ".claude/skills/example-skill.md",
          }),
        ],
      }),
    ),
  });
  const profiles = fakeProfileApi();
  await act(async () =>
    render(
      <NativeAssets initialKind="skill" api={api} profileApi={profiles} onCreated={vi.fn()} />,
    ),
  );
  expect(screen.getByRole("button", { name: "Skills" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByText("example-skill")).toBeTruthy();
  expect(screen.queryByText("example-agent")).toBeNull();
  expect(screen.getByText("1 shown / 2 returned · Your Claude folder")).toBeTruthy();
  expect(api.sources).toHaveBeenCalledTimes(1);
  expect(api.catalog).toHaveBeenCalledTimes(1);
  expect(profiles.create).not.toHaveBeenCalled();
});

it("the mount seed preserves manual filters and valid sources until a new keyed entry", async () => {
  const api = fakeApi({
    sources: vi.fn(async () => [userSource, vaultSource]),
    catalog: vi.fn(async (sourceId) => {
      const sourceScope = sourceId === "vault:1" ? "workspace" : "user";
      return catalog({
        source_id: sourceId,
        source_scope: sourceScope,
        assets: [
          asset({ source_id: sourceId, source_scope: sourceScope, name: "Release agent" }),
          asset({
            source_id: sourceId,
            source_scope: sourceScope,
            kind: "skill",
            name: "Release skill",
            relative_id: ".claude/skills/release/SKILL.md",
          }),
        ],
      });
    }),
  });
  const profiles = fakeProfileApi();
  const onCreated = vi.fn();
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(
      <NativeAssets initialKind="skill" api={api} profileApi={profiles} onCreated={onCreated} />,
    );
  });
  fireEvent.click(screen.getByRole("button", { name: "Subagents" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Search native assets" }), {
    target: { value: "release" },
  });
  await act(async () =>
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "claude:user" } }),
  );
  const refreshedApi = { ...api };
  await act(async () =>
    view.rerender(
      <NativeAssets
        initialKind="all"
        api={refreshedApi}
        profileApi={profiles}
        onCreated={onCreated}
      />,
    ),
  );
  await act(async () =>
    view.rerender(
      <NativeAssets
        initialKind="skill"
        api={refreshedApi}
        profileApi={profiles}
        onCreated={onCreated}
      />,
    ),
  );
  expect(screen.getByRole("button", { name: "Subagents" }).getAttribute("aria-pressed")).toBe(
    "true",
  );
  expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("release");
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("claude:user");
  expect(api.catalog).toHaveBeenLastCalledWith("claude:user", expect.anything());
  expect(screen.getByText("Release agent")).toBeTruthy();
  expect(screen.queryByText("Release skill")).toBeNull();
  const nextContextApi = { ...api, sources: vi.fn(async () => [vaultSource]) };
  await act(async () =>
    view.rerender(
      <NativeAssets
        initialKind="skill"
        api={nextContextApi}
        profileApi={profiles}
        onCreated={onCreated}
      />,
    ),
  );
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("vault:1");
  expect(api.catalog).toHaveBeenLastCalledWith("vault:1", expect.anything());
  expect(screen.getByRole("button", { name: "Subagents" }).getAttribute("aria-pressed")).toBe(
    "true",
  );
  expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("release");
  await act(async () =>
    view.rerender(
      <NativeAssets
        key="skills-entry"
        initialKind="skill"
        api={nextContextApi}
        profileApi={profiles}
        onCreated={onCreated}
      />,
    ),
  );
  expect(screen.getByRole("button", { name: "Skills" }).getAttribute("aria-pressed")).toBe("true");
  expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("");
  expect(screen.getByText("Release skill")).toBeTruthy();
  expect(screen.queryByText("Release agent")).toBeNull();
  expect(profiles.create).not.toHaveBeenCalled();
});

it("workspace skills are discoverable by name, description and path", async () => {
  const skill = asset({
    source_id: "vault:1",
    source_scope: "workspace",
    kind: "skill",
    name: "Release checklist",
    description: "Ship the next version.",
    relative_id: ".claude/skills/release/SKILL.md",
  });
  const api = fakeApi({
    sources: vi.fn(async () => [userSource, vaultSource]),
    catalog: vi.fn(async (id) =>
      id === "vault:1"
        ? catalog({ source_id: id, source_scope: "workspace", assets: [asset(), skill] })
        : catalog(),
    ),
  });
  const profiles = fakeProfileApi();
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(<NativeAssets api={api} profileApi={profiles} onCreated={vi.fn()} />);
  });
  expect(api.catalog).toHaveBeenCalledWith("vault:1", expect.anything());
  fireEvent.click(screen.getByRole("button", { name: "Skills" }));
  const row = within(screen.getByText("Release checklist").closest("li") as HTMLElement);
  expect(row.getByText(skill.relative_id)).toBeTruthy();
  expect(row.getByText("This vault")).toBeTruthy();
  expect(row.getByText(new RegExp(skill.digest)).closest("details")).not.toBeNull();
  expect(screen.queryByText("Create an app profile from this agent")).toBeNull();
  for (const query of ["release", "ship", ".claude/skills/release"]) {
    fireEvent.change(screen.getByRole("textbox", { name: "Search native assets" }), {
      target: { value: query },
    });
    expect(screen.getByText("Release checklist")).toBeTruthy();
    expect(screen.queryByText("example-agent")).toBeNull();
    expect(screen.getByText("1 shown / 2 returned · This vault")).toBeTruthy();
  }
  expect(api.catalog).toHaveBeenCalledTimes(1);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Subagents" }));
  expect(screen.getByText("example-agent")).toBeTruthy();
  expect(screen.queryByText("Release checklist")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  expect(screen.getByText("Release checklist")).toBeTruthy();
  await act(async () =>
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "claude:user" } }),
  );
  await act(async () =>
    view.rerender(<NativeAssets api={{ ...api }} profileApi={profiles} onCreated={vi.fn()} />),
  );
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("claude:user");
  expect(api.catalog).toHaveBeenLastCalledWith("claude:user", expect.anything());
  expect(profiles.create).not.toHaveBeenCalled();
});

it("partial count is qualified even when a local search has no matches", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({ partial: true, partial_reasons: ["unsafe_entry"], omitted: 0 }),
    ),
  });
  await act(async () =>
    render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />),
  );
  expect(
    screen.getByText("1 shown / 1 returned · Your Claude folder · Partial catalog"),
  ).toBeTruthy();
  expect(screen.getByText("Some entries were skipped for safety.")).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox", { name: "Search native assets" }), {
    target: { value: "absent" },
  });
  expect(
    screen.getByText("0 shown / 1 returned · Your Claude folder · Partial catalog"),
  ).toBeTruthy();
  expect(screen.getByText("No matching native assets")).toBeTruthy();
});

it.each(["sources", "catalog"] as const)(
  "offers explicit Retry for a failed %s read",
  async (method) => {
    const api = fakeApi();
    vi.mocked(api[method]).mockRejectedValueOnce(new Error("failed read"));
    await act(async () =>
      render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />),
    );
    expect(screen.queryByText("example-agent")).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry" })));
    expect(screen.getByText("example-agent")).toBeTruthy();
    expect(api[method]).toHaveBeenCalledTimes(2);
    expect(api[method === "sources" ? "catalog" : "sources"]).toHaveBeenCalledTimes(1);
  },
);

it("a partial catalog with malformed_metadata and no omitted count shows a neutral reason", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () => catalog({ partial: true, partial_reasons: ["malformed_metadata"] })),
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("Some entries' details were not in the expected format.");
});

it("a partial catalog with malformed_metadata and omitted 6 names the count", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({ partial: true, partial_reasons: ["malformed_metadata"], omitted: 6 }),
    ),
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  const line = await screen.findByText(/details were not in the expected format\./);
  expect(line.textContent).toBe("6 entries' details were not in the expected format.");
  expect(screen.queryByText(/One entry/)).toBeNull();
});

it("one oversized entry reads in the singular", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({ partial: true, partial_reasons: ["oversized"], omitted: 1 }),
    ),
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("1 entry was too large to read in full.");
});

it("a whole folder left out (unsafe_entry, omitted 0) reads as some entries", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({ partial: true, partial_reasons: ["unsafe_entry"], omitted: 0 }),
    ),
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("Some entries were skipped for safety.");
});

it("one malformed entry reads in the singular possessive", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({ partial: true, partial_reasons: ["malformed_metadata"], omitted: 1 }),
    ),
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("1 entry's details were not in the expected format.");
});

it("two partial reasons with omitted 6 never attribute the shared count to either line", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({
        partial: true,
        partial_reasons: ["malformed_metadata", "oversized"],
        omitted: 6,
      }),
    ),
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("Some entries' details were not in the expected format.");
  await screen.findByText("Some entries were too large to read in full.");
  expect(screen.queryByText(/6 entr/)).toBeNull();
});

it("a sources failure shows the error state", async () => {
  const api = fakeApi({ sources: vi.fn(() => Promise.reject(new Error("boom"))) });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("Native assets are unavailable.");
});

it("unmounting before sources() resolves aborts its signal", () => {
  let capturedSignal: AbortSignal | undefined;
  const api = fakeApi({
    sources: vi.fn(
      (signal?: AbortSignal) =>
        new Promise<NativeSource[]>(() => {
          capturedSignal = signal;
        }),
    ),
  });
  const { unmount } = render(
    <NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />,
  );
  unmount();
  expect(capturedSignal?.aborted).toBe(true);
});

it("creates an app profile from an agent, mapping inherit to a null model", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () => catalog({ assets: [asset({ model: "inherit" })] })),
  });
  const create = vi.fn<ProfileApi["create"]>(async () => ({
    profile_id: "new-profile",
    revision: 1,
    state_revision: 1,
  }));
  const onCreated = vi.fn();
  render(<NativeAssets api={api} profileApi={fakeProfileApi({ create })} onCreated={onCreated} />);
  fireEvent.click(await screen.findByText("Create an app profile from this agent"));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith("new-profile"));
  expect(create).toHaveBeenCalledTimes(1);
  const call = create.mock.calls[0];
  expect(typeof call?.[0]).toBe("string");
  expect(typeof call?.[1]).toBe("string");
  expect(call?.[2].runtime.model).toBeNull();
  expect(call?.[2].identity.name).toBe("example-agent");
  expect(call?.[2].provenance).toEqual({
    kind: "native",
    derived_from: null,
    native_source: {
      source_id: "claude:user",
      relative_id: ".claude/agents/example-agent.md",
      digest: "a".repeat(64),
      observed_at: "2026-09-28T00:00:00.000Z",
    },
  });
});

it("a failed create keeps the minted ids for the retry", async () => {
  const api = fakeApi();
  const create = vi
    .fn<ProfileApi["create"]>()
    .mockRejectedValueOnce(new ProfileApiError(409, "command_conflict"))
    .mockResolvedValueOnce({ profile_id: "p1", revision: 1, state_revision: 1 });
  render(<NativeAssets api={api} profileApi={fakeProfileApi({ create })} onCreated={vi.fn()} />);
  const button = await screen.findByText("Create an app profile from this agent");
  fireEvent.click(button);
  await screen.findByText("This retry no longer matches the original request.");
  fireEvent.click(screen.getByText("Create an app profile from this agent"));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  const first = create.mock.calls[0];
  const second = create.mock.calls[1];
  expect(second?.[0]).toBe(first?.[0]);
  expect(second?.[1]).toBe(first?.[1]);
});

it("a catalog reload between attempts still retries the first document", async () => {
  let userCalls = 0;
  const catalogFn = vi.fn<NativeAssetsApi["catalog"]>(async (sourceId: string) => {
    if (sourceId === "vault:1") {
      return catalog({ source_id: "vault:1", source_scope: "workspace", assets: [] });
    }
    userCalls += 1;
    return catalog({
      assets: [
        asset({
          observed_at: userCalls === 1 ? "2026-09-28T00:00:00.000Z" : "2026-09-29T00:00:00.000Z",
        }),
      ],
    });
  });
  const api = fakeApi({
    sources: vi.fn(async () => [userSource, vaultSource]),
    catalog: catalogFn,
  });
  const create = vi
    .fn<ProfileApi["create"]>()
    .mockRejectedValueOnce(new ProfileApiError(409, "command_conflict"))
    .mockResolvedValueOnce({ profile_id: "p1", revision: 1, state_revision: 1 });
  render(<NativeAssets api={api} profileApi={fakeProfileApi({ create })} onCreated={vi.fn()} />);
  fireEvent.change(await screen.findByRole("combobox", {}, { timeout: 50 }), {
    target: { value: "claude:user" },
  });
  fireEvent.click(await screen.findByText("Create an app profile from this agent"));
  await screen.findByText("This retry no longer matches the original request.");
  // Switching source and back reloads the catalog, changing the agent's observed_at.
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "vault:1" } });
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "claude:user" } });
  await waitFor(() => expect(userCalls).toBe(2));
  await screen.findByText(/2026-09-29/);
  fireEvent.click(screen.getByText("Create an app profile from this agent"));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  expect(create.mock.calls[1]?.[2]).toEqual(create.mock.calls[0]?.[2]);
});

it("an earlier source's catalog never replaces a newer selection", async () => {
  let resolveUser: (value: NativeCatalog) => void = () => {};
  let resolveVault: (value: NativeCatalog) => void = () => {};
  const userCatalogPromise = new Promise<NativeCatalog>((resolve) => {
    resolveUser = resolve;
  });
  const vaultCatalogPromise = new Promise<NativeCatalog>((resolve) => {
    resolveVault = resolve;
  });
  const catalogFn = vi.fn<NativeAssetsApi["catalog"]>((sourceId: string) =>
    sourceId === "claude:user" ? userCatalogPromise : vaultCatalogPromise,
  );
  const api = fakeApi({
    sources: vi.fn(async () => [userSource, vaultSource]),
    catalog: catalogFn,
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await waitFor(() => expect(catalogFn).toHaveBeenCalledWith("vault:1", expect.anything()), {
    timeout: 50,
  });
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "claude:user" } });
  await waitFor(() => expect(catalogFn).toHaveBeenCalledWith("claude:user", expect.anything()), {
    timeout: 50,
  });
  await act(async () => {
    resolveUser(catalog({ source_id: "claude:user", assets: [asset({ name: "user-agent" })] }));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByText("user-agent")).toBeTruthy();
  await act(async () => {
    resolveVault(catalog({ source_id: "vault:1", assets: [asset({ name: "vault-agent" })] }));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.queryByText("vault-agent")).toBeNull();
  expect(screen.getByText("user-agent")).toBeTruthy();
});

it("skills have no create button", async () => {
  const api = fakeApi({
    catalog: vi.fn(async () =>
      catalog({
        assets: [asset({ kind: "skill", name: "example-skill" })],
      }),
    ),
  });
  render(<NativeAssets api={api} profileApi={fakeProfileApi()} onCreated={vi.fn()} />);
  await screen.findByText("example-skill");
  expect(screen.queryByText("Create an app profile from this agent")).toBeNull();
});
