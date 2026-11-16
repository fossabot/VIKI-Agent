import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  type NativeAssetsApi,
  NativeAssetsApiError,
  type NativeAssetView,
  type NativeCatalog,
  type NativeSource,
} from "../../clients/profiles/nativeAssetsApi";
import { NativeCapabilities } from "./NativeCapabilities";

afterEach(cleanup);

const workspace: NativeSource = {
  source_id: "workspace:a",
  provider: "claude",
  scope: "workspace",
};
const user: NativeSource = { source_id: "user:a", provider: "claude", scope: "user" };

function skill(source = workspace, extra: Partial<NativeAssetView> = {}): NativeAssetView {
  return {
    source_id: source.source_id,
    source_scope: source.scope,
    provider: "claude",
    relative_id: "skills/example/SKILL.md",
    kind: "skill",
    name: "Example",
    description: null,
    model: null,
    tools: [],
    digest: "a".repeat(64),
    observed_at: "2026-10-12T00:00:00.000Z",
    supported_fields: ["name"],
    unsupported_fields: [],
    launch_supported: false,
    ...extra,
  };
}

function catalog(source = workspace, extra: Partial<NativeCatalog> = {}): NativeCatalog {
  return {
    source_id: source.source_id,
    source_scope: source.scope,
    provider: "claude",
    assets: [skill(source)],
    partial: false,
    partial_reasons: [],
    scanned_entries: 1,
    scanned_bytes: 128,
    omitted: 0,
    ...extra,
  };
}

function api(extra: Partial<NativeAssetsApi> = {}): NativeAssetsApi {
  return {
    sources: vi.fn(async () => [user, workspace]),
    catalog: vi.fn(async (sourceId) => catalog(sourceId === user.source_id ? user : workspace)),
    ...extra,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

async function read(client: NativeAssetsApi, onManage?: () => void) {
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(<NativeCapabilities api={client} onManage={onManage} />);
  });
  return view;
}

test("browses only returned skills with exact identities and local detail selection", async () => {
  const client = api({
    catalog: vi.fn(async () =>
      catalog(workspace, {
        assets: [
          skill(workspace, { relative_id: "skills/one/SKILL.md", name: "<b>Same</b>" }),
          skill(workspace, { relative_id: "skills/two/SKILL.md", name: "<b>Same</b>" }),
          skill(workspace, { kind: "agent", name: "Hidden agent" }),
        ],
      }),
    ),
  });
  const onManage = vi.fn();
  const view = await read(client, onManage);
  expect(client.catalog).toHaveBeenCalledWith(workspace.source_id, expect.any(AbortSignal));
  const list = screen.getByRole("list", { name: "Returned native skills" });
  const rows = within(list).getAllByRole("button");
  expect(rows).toHaveLength(2);
  expect(rows[0]?.textContent).toContain("Ref: skills/one/SKILL.md");
  expect(rows[1]?.textContent).toContain("Ref: skills/two/SKILL.md");
  expect(rows.every((row) => row.textContent?.includes("Source: workspace:a"))).toBe(true);
  expect(screen.queryByText("Hidden agent")).toBeNull();
  expect(view.container.querySelector("b")).toBeNull();
  expect(screen.queryByRole("region", { name: "Selected skill" })).toBeNull();
  rows[1]?.focus();
  expect(document.activeElement).toBe(rows[1]);
  fireEvent.click(rows[1] as HTMLElement);
  const detail = screen.getByRole("region", { name: "Selected skill" });
  expect(within(detail).getByText("Ref: skills/two/SKILL.md")).toBeTruthy();
  expect(within(detail).getByText("Source: workspace:a")).toBeTruthy();
  expect(rows[1]?.getAttribute("aria-pressed")).toBe("true");
  expect(within(detail).getByText("Launch from this catalog is unsupported.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Manage native capabilities" }));
  expect(onManage).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("switch")).toBeNull();
});

test("searches returned refs and descriptions without extra reads or changing coverage", async () => {
  const client = api({
    catalog: vi.fn(async () =>
      catalog(workspace, {
        assets: [
          skill(),
          skill(workspace, { relative_id: "skills/other/SKILL.md", description: "Trace issue" }),
        ],
        partial: true,
        partial_reasons: ["oversized", "entry_limit"],
        omitted: 2,
      }),
    ),
  });
  await read(client);
  const input = screen.getByRole("searchbox", { name: "Search native skills" });
  fireEvent.change(input, { target: { value: "trace" } });
  expect(screen.getByText("Skills shown: 1 of 2 returned")).toBeTruthy();
  expect(screen.getByText("Catalog coverage: partial")).toBeTruthy();
  expect(screen.getByText("Omitted catalog entries: 2")).toBeTruthy();
  expect(screen.getByText(/oversized/)).toBeTruthy();
  expect(screen.getByText(/entry_limit/)).toBeTruthy();
  fireEvent.change(input, { target: { value: "missing" } });
  expect(screen.getByText("No returned skills match this search.")).toBeTruthy();
  fireEvent.change(input, { target: { value: "skills/example/SKILL.md" } });
  expect(screen.getByText("Skills shown: 1 of 2 returned")).toBeTruthy();
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
});

test.each([true, false])("known empty catalog distinguishes partial=%s", async (partial) => {
  await read(api({ catalog: vi.fn(async () => catalog(workspace, { assets: [], partial })) }));
  expect(screen.getByText("Skills shown: 0 of 0 returned")).toBeTruthy();
  expect(
    screen.getByText(
      partial
        ? "No skills were returned; this catalog is partial."
        : "No skills were returned by this complete catalog.",
    ),
  ).toBeTruthy();
});

test.each(["sources", "catalog"] as const)(
  "failed %s remains unknown and supports deliberate retry",
  async (stage) => {
    const client = api();
    vi.mocked(client[stage]).mockRejectedValueOnce(
      new NativeAssetsApiError(409, "source_unavailable"),
    );
    await read(client);
    expect(screen.getByRole("alert").textContent).toContain("source_unavailable");
    expect(screen.getByText("Skills shown: unknown")).toBeTruthy();
    expect(screen.queryByText(/No skills were returned/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await act(async () => {});
    expect(screen.getByText("Skills shown: 1 of 1 returned")).toBeTruthy();
  },
);

test.each([true, false])(
  "source switch cancels and ignores stale catalog failure=%s",
  async (failure) => {
    const old = deferred<NativeCatalog>();
    const client = api({
      catalog: vi.fn((sourceId) =>
        sourceId === workspace.source_id ? old.promise : Promise.resolve(catalog(user)),
      ),
    });
    await read(client);
    const signal = vi.mocked(client.catalog).mock.calls[0]?.[1];
    fireEvent.change(screen.getByRole("combobox", { name: "Native source" }), {
      target: { value: user.source_id },
    });
    await act(async () => {});
    expect(signal?.aborted).toBe(true);
    fireEvent.click(within(screen.getByRole("list")).getByRole("button"));
    await act(async () => {
      if (failure) old.reject(new Error("synthetic stale failure"));
      else old.resolve(catalog(workspace, { assets: [skill(workspace, { name: "Stale skill" })] }));
    });
    expect(screen.queryByText("Stale skill")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      within(screen.getByRole("region", { name: "Selected skill" })).getByText("Source: user:a"),
    ).toBeTruthy();
  },
);

test("source refresh preserves exact selection when a source disappears", async () => {
  const client = api({
    sources: vi.fn().mockResolvedValueOnce([workspace, user]).mockResolvedValueOnce([workspace]),
  });
  await read(client);
  fireEvent.change(screen.getByRole("combobox", { name: "Native source" }), {
    target: { value: user.source_id },
  });
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Refresh sources" }));
  await act(async () => {});
  expect((screen.getByRole("combobox", { name: "Native source" }) as HTMLSelectElement).value).toBe(
    user.source_id,
  );
  expect(screen.getByText("Selected source is unavailable. Choose another source.")).toBeTruthy();
  expect(screen.queryByRole("list")).toBeNull();
  expect(client.catalog).toHaveBeenCalledTimes(2);
});

test.each(["sources", "catalog"] as const)(
  "unmount aborts pending %s and ignores completion",
  async (stage) => {
    const sources = deferred<NativeSource[]>();
    const entries = deferred<NativeCatalog>();
    const client = api(
      stage === "sources"
        ? { sources: vi.fn(() => sources.promise) }
        : { catalog: vi.fn(() => entries.promise) },
    );
    const view = await read(client);
    const signal =
      stage === "sources"
        ? vi.mocked(client.sources).mock.calls[0]?.[0]
        : vi.mocked(client.catalog).mock.calls[0]?.[1];
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => {
      sources.resolve([workspace]);
      entries.resolve(catalog());
    });
    if (stage === "sources") expect(client.catalog).not.toHaveBeenCalled();
  },
);

test("API replacement hides old details and rejects late catalog completion", async () => {
  const old = deferred<NativeCatalog>();
  const client = api({ catalog: vi.fn(() => old.promise) });
  const view = await read(client);
  const signal = vi.mocked(client.catalog).mock.calls[0]?.[1];
  const fresh = api({
    sources: vi.fn(async () => [workspace]),
    catalog: vi.fn(async () =>
      catalog(workspace, { assets: [skill(workspace, { name: "Fresh" })] }),
    ),
  });
  view.rerender(<NativeCapabilities api={fresh} />);
  expect(screen.queryByRole("list")).toBeNull();
  await act(async () => {});
  expect(signal?.aborted).toBe(true);
  await act(async () =>
    old.resolve(catalog(workspace, { assets: [skill(workspace, { name: "Old" })] })),
  );
  expect(screen.queryByText("Old")).toBeNull();
  expect(screen.getByText("Fresh")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Selected skill" })).toBeNull();
});

test("empty sources do not invent a successful zero catalog", async () => {
  const client = api({ sources: vi.fn(async () => []) });
  await read(client);
  expect(screen.getByText("No native sources are available.")).toBeTruthy();
  expect(screen.getByText("Skills shown: unknown")).toBeTruthy();
  expect(client.catalog).not.toHaveBeenCalled();
});

test("API replacement aborts source reads and ignores old source arrival", async () => {
  const old = deferred<NativeSource[]>();
  const client = api({ sources: vi.fn(() => old.promise) });
  const view = await read(client);
  const signal = vi.mocked(client.sources).mock.calls[0]?.[0];
  const fresh = api({ sources: vi.fn(async () => [user]) });
  view.rerender(<NativeCapabilities api={fresh} />);
  await act(async () => {});
  expect(signal?.aborted).toBe(true);
  await act(async () => old.resolve([workspace]));
  expect(client.catalog).not.toHaveBeenCalled();
  expect(fresh.catalog).toHaveBeenCalledWith(user.source_id, expect.any(AbortSignal));
  expect(within(screen.getByRole("list")).getByText("Source: user:a")).toBeTruthy();
});

test("catalog refresh keeps exact selection instead of choosing a different duplicate title", async () => {
  const first = skill(workspace, { relative_id: "skills/one/SKILL.md" });
  const second = skill(workspace, { relative_id: "skills/two/SKILL.md" });
  const client = api({
    catalog: vi
      .fn()
      .mockResolvedValueOnce(catalog(workspace, { assets: [first, second], partial: true }))
      .mockResolvedValueOnce(catalog(workspace, { assets: [first] })),
  });
  await read(client);
  fireEvent.click(within(screen.getByRole("list")).getAllByRole("button")[1] as HTMLElement);
  expect(screen.getByRole("region", { name: "Selected skill" }).textContent).toContain(
    second.relative_id,
  );
  fireEvent.click(screen.getByRole("button", { name: "Refresh catalog" }));
  await act(async () => {});
  expect(screen.queryByRole("region", { name: "Selected skill" })).toBeNull();
  expect(within(screen.getByRole("list")).getByRole("button").getAttribute("aria-pressed")).toBe(
    "false",
  );
});
