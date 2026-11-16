import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type {
  NativeAssetsApi,
  NativeAssetView,
  NativeCatalog,
  NativeSource,
} from "../../clients/profiles/nativeAssetsApi";
import { PanelHost } from "../Shell/PanelHost";
import { SkillsSummary } from "./SkillsSummary";

afterEach(cleanup);

const user: NativeSource = { source_id: "user:a", provider: "claude", scope: "user" };
const workspace: NativeSource = {
  source_id: "workspace:a",
  provider: "claude",
  scope: "workspace",
};

function asset(kind: NativeAssetView["kind"], source: NativeSource): NativeAssetView {
  return {
    source_id: source.source_id,
    source_scope: source.scope,
    provider: "claude",
    relative_id: kind === "skill" ? "skills/example/SKILL.md" : "agents/example.md",
    kind,
    name: "Example",
    description: null,
    model: null,
    tools: [],
    digest: "a".repeat(64),
    observed_at: "2026-10-04T00:00:00.000Z",
    supported_fields: [],
    unsupported_fields: [],
    launch_supported: false,
  };
}

function catalog(source = workspace, extra: Partial<NativeCatalog> = {}): NativeCatalog {
  return {
    source_id: source.source_id,
    provider: "claude",
    source_scope: source.scope,
    assets: [],
    partial: false,
    partial_reasons: [],
    scanned_entries: 2,
    scanned_bytes: 128,
    omitted: 0,
    ...extra,
  };
}

function api(extra: Partial<NativeAssetsApi> = {}): NativeAssetsApi {
  return {
    sources: vi.fn(async () => [workspace]),
    catalog: vi.fn(async () => catalog()),
    ...extra,
  };
}

async function read(client: NativeAssetsApi, onOpen = vi.fn()) {
  await act(async () =>
    render(<SkillsSummary api={client} contextKey="workspace:1" onOpen={onOpen} />),
  );
}

test.each([
  { sources: [user, workspace], selected: workspace, label: "Workspace" },
  { sources: [user], selected: user, label: "User" },
])("counts only Skills from the preferred $label source and opens once", async (input) => {
  const onOpen = vi.fn();
  const client = api({
    sources: vi.fn(async () => input.sources),
    catalog: vi.fn(async () =>
      catalog(input.selected, {
        assets: [asset("agent", input.selected), asset("skill", input.selected)],
      }),
    ),
  });
  await read(client, onOpen);
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledWith(input.selected.source_id, expect.any(AbortSignal));
  expect(screen.getByText(`${input.label} source: ${input.selected.source_id}`)).toBeTruthy();
  expect(screen.getByText("Skills returned: 1")).toBeTruthy();
  expect(screen.getByText("Catalog coverage: complete")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Open Skills" }));
  expect(onOpen).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
});

test.each([false, true])(
  "distinguishes a successful empty catalog with partial=%s",
  async (partial) => {
    await read(
      api({
        catalog: vi.fn(async () =>
          catalog(workspace, {
            partial,
            partial_reasons: partial ? ["entry_limit", "unreadable"] : [],
            omitted: partial ? 3 : 0,
          }),
        ),
      }),
    );
    expect(screen.getByText("Skills returned: 0")).toBeTruthy();
    expect(screen.getByText(`Catalog coverage: ${partial ? "partial" : "complete"}`)).toBeTruthy();
    expect(
      screen.getByText(
        partial
          ? "No skills were returned; this catalog is partial."
          : "No skills were returned by this complete catalog.",
      ),
    ).toBeTruthy();
    if (partial) {
      expect(screen.getByText("Omitted catalog entries: 3")).toBeTruthy();
      expect(screen.getByText("entry_limit")).toBeTruthy();
      expect(screen.getByText("unreadable")).toBeTruthy();
    }
    expect(screen.queryByRole("alert")).toBeNull();
  },
);

test.each(["sources", "catalog"] as const)(
  "failed %s reads never report a successful zero",
  async (stage) => {
    const client = api({
      [stage]: vi.fn(async () => {
        throw new Error("synthetic read failure");
      }),
    });
    await read(client);
    expect(screen.getByRole("alert").textContent).toBe(
      stage === "sources"
        ? "Skill sources could not be read."
        : "Skills catalog could not be read.",
    );
    expect(screen.queryByText(/Skills returned:/)).toBeNull();
    expect(screen.queryByText(/No skills were returned/)).toBeNull();
    if (stage === "sources") expect(client.catalog).not.toHaveBeenCalled();
  },
);

test("a successful no-source response does not invent an empty catalog", async () => {
  const client = api({ sources: vi.fn(async () => []) });
  await read(client);
  expect(screen.getByText("No native sources are available.")).toBeTruthy();
  expect(client.catalog).not.toHaveBeenCalled();
  expect(screen.queryByText(/Skills returned:/)).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

test("source Retry is deliberate and disabled while the source read is pending", async () => {
  const pending = deferred<NativeSource[]>();
  const client = api({
    sources: vi
      .fn()
      .mockRejectedValueOnce(new Error("synthetic source failure"))
      .mockReturnValueOnce(pending.promise),
  });
  await read(client);
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect((screen.getByRole("button", { name: "Retry" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("Loading skill sources…")).toBeTruthy();
  expect(client.sources).toHaveBeenCalledTimes(2);
  expect(client.catalog).not.toHaveBeenCalled();
  await act(async () => pending.resolve([workspace]));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("Skills returned: 0")).toBeTruthy();
  expect(client.sources).toHaveBeenCalledTimes(2);
  expect(client.catalog).toHaveBeenCalledTimes(1);
});

test("partial catalog Retry retains last-read truth through failure and clears it on success", async () => {
  const pending = deferred<NativeCatalog>();
  const last = catalog(workspace, {
    assets: [asset("skill", workspace)],
    partial: true,
    partial_reasons: ["entry_limit"],
    omitted: 3,
  });
  const client = api({
    catalog: vi
      .fn()
      .mockResolvedValueOnce(last)
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(catalog()),
  });
  await read(client);
  const details = screen.getByText("entry_limit").closest("details");
  expect(details).toBeTruthy();
  expect(details?.hasAttribute("open")).toBe(false);
  expect(screen.getByText("Omitted catalog entries: 3").closest("details")).toBe(details);
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect((screen.getByRole("button", { name: "Retry" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("Skills returned: 1")).toBeTruthy();
  expect(screen.getByText("Workspace source: workspace:a")).toBeTruthy();
  expect(screen.getByText("Last successful read; current catalog read is pending.")).toBeTruthy();
  await act(async () => pending.reject(new Error("synthetic catalog failure")));
  expect(screen.getByRole("alert").textContent).toBe("Skills catalog could not be read.");
  expect(screen.getByText("Skills returned: 1")).toBeTruthy();
  expect(screen.getByText("Catalog coverage: partial")).toBeTruthy();
  expect(screen.getByText("Last successful read; current catalog read failed.")).toBeTruthy();
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await act(async () => {});
  expect(screen.getByText("Skills returned: 0")).toBeTruthy();
  expect(screen.getByText("Catalog coverage: complete")).toBeTruthy();
  expect(screen.queryByText(/Last successful read/)).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(3);
});

test.each([
  { boundary: "api", failure: false },
  { boundary: "api", failure: true },
  { boundary: "context", failure: false },
  { boundary: "context", failure: true },
])(
  "$boundary clears retained data and ignores obsolete failure=$failure",
  async ({ boundary, failure }) => {
    const pending = deferred<NativeCatalog>();
    const next = deferred<NativeCatalog>();
    const initial = catalog(workspace, { assets: [asset("skill", workspace)], partial: true });
    const client = api({
      sources: vi.fn().mockResolvedValueOnce([workspace]).mockResolvedValue([user]),
      catalog: vi
        .fn()
        .mockResolvedValueOnce(initial)
        .mockReturnValueOnce(pending.promise)
        .mockReturnValueOnce(next.promise),
    });
    const view = render(<SkillsSummary api={client} contextKey="workspace:1" onOpen={vi.fn()} />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.getByText("Skills returned: 1")).toBeTruthy();
    const oldSignal = vi.mocked(client.catalog).mock.calls[1]?.[1];
    const fresh =
      boundary === "api"
        ? api({ sources: vi.fn(async () => [user]), catalog: vi.fn(() => next.promise) })
        : client;
    view.rerender(
      <SkillsSummary
        api={fresh}
        contextKey={boundary === "api" ? "workspace:1" : "workspace:2"}
        onOpen={vi.fn()}
      />,
    );
    expect(screen.queryByText(/Skills returned:/)).toBeNull();
    expect(screen.queryByText(/Last successful read/)).toBeNull();
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => next.resolve(catalog(user)));
    expect(screen.getByText("User source: user:a")).toBeTruthy();
    expect(screen.getByText("Skills returned: 0")).toBeTruthy();
    await act(async () => {
      if (failure) pending.reject(new Error("obsolete catalog failure"));
      else pending.resolve(initial);
    });
    expect(screen.getByText("User source: user:a")).toBeTruthy();
    expect(screen.getByText("Skills returned: 0")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Last successful read/)).toBeNull();
  },
);

test("ignored-abort source completion after unmount never starts a catalog", async () => {
  const pending = deferred<NativeSource[]>();
  const client = api({ sources: vi.fn(() => pending.promise) });
  const view = render(<SkillsSummary api={client} contextKey="workspace:1" onOpen={vi.fn()} />);
  const signal = vi.mocked(client.sources).mock.calls[0]?.[0];
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => pending.resolve([workspace]));
  expect(client.catalog).not.toHaveBeenCalled();
});

test("compact count and coverage stay outside collapsed long source details", async () => {
  const longSource = { ...workspace, source_id: `workspace:${"long name".repeat(80)}<b>` };
  const onOpen = vi.fn();
  const client = api({
    sources: vi.fn(async () => [longSource]),
    catalog: vi.fn(async () =>
      catalog(longSource, {
        assets: [asset("agent", longSource), asset("skill", longSource)],
        partial: true,
        omitted: 2,
        partial_reasons: ["entry_limit"],
      }),
    ),
  });
  const view = render(
    <SkillsSummary api={client} contextKey="workspace:1" compact onOpen={onOpen} />,
  );
  await act(async () => {});
  const details = view.container.querySelector("details");
  expect(details?.open).toBe(false);
  expect(screen.getByText("Skills returned: 1").closest("details")).toBeNull();
  expect(screen.getByText("Catalog coverage: partial").closest("details")).toBeNull();
  expect(screen.getByText(`Workspace source: ${longSource.source_id}`).closest("details")).toBe(
    details,
  );
  expect(screen.getByText("Omitted catalog entries: 2").closest("details")).toBeTruthy();
  expect(view.container.querySelector("b")).toBeNull();
  if (!details) throw new Error("Missing Skills details");
  details.open = true;
  fireEvent.click(screen.getByRole("button", { name: "Open Skills" }));
  expect(onOpen).toHaveBeenCalledTimes(1);
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
});

test.each(["sources", "catalog", "empty", "pending"] as const)(
  "compact %s absence or failure remains an unknown count",
  async (stage) => {
    const client = api(
      stage === "pending"
        ? { sources: vi.fn(() => deferred<NativeSource[]>().promise) }
        : stage === "empty"
          ? { sources: vi.fn(async () => []) }
          : {
              [stage]: vi.fn(async () => {
                throw new Error("synthetic read failure");
              }),
            },
    );
    render(<SkillsSummary api={client} contextKey="workspace:1" compact onOpen={vi.fn()} />);
    await act(async () => {});
    expect(screen.getByText("Skills returned: unknown").closest("details")).toBeNull();
    expect(screen.queryByText("Skills returned: 0")).toBeNull();
    expect(screen.queryByText(/Catalog coverage:/)).toBeNull();
    if (stage === "empty")
      expect(screen.getByText("No sources available.").closest("details")).toBeNull();
    else
      expect(
        screen
          .getByText(stage === "pending" ? /Read pending\./ : /Read failed\./)
          .closest("details"),
      ).toBeNull();
    if (stage !== "catalog") expect(client.catalog).not.toHaveBeenCalled();
  },
);

test.each([false, true])("compact empty catalog keeps coverage partial=%s", async (partial) => {
  render(
    <SkillsSummary
      api={api({ catalog: vi.fn(async () => catalog(workspace, { partial })) })}
      contextKey="workspace:1"
      compact
      onOpen={vi.fn()}
    />,
  );
  await act(async () => {});
  expect(screen.getByText("Skills returned: 0").closest("details")).toBeNull();
  expect(
    screen.getByText(`Catalog coverage: ${partial ? "partial" : "complete"}`).closest("details"),
  ).toBeNull();
  expect(
    screen
      .getByText(
        partial
          ? "No skills were returned; this catalog is partial."
          : "No skills were returned by this complete catalog.",
      )
      .closest("details"),
  ).toBeTruthy();
});

test("compact Retry retains count and freshness outside details through pending and failure", async () => {
  const pending = deferred<NativeCatalog>();
  const client = api({
    catalog: vi
      .fn()
      .mockResolvedValueOnce(
        catalog(workspace, {
          assets: [asset("skill", workspace)],
          partial: true,
        }),
      )
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(catalog()),
  });
  render(<SkillsSummary api={client} contextKey="workspace:1" compact onOpen={vi.fn()} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(screen.getByText("Skills returned: 1").closest("details")).toBeNull();
  expect(
    screen.getByText("Last successful read; current catalog read is pending.").closest("details"),
  ).toBeNull();
  expect((screen.getByRole("button", { name: "Retry" }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => pending.reject(new Error("synthetic catalog failure")));
  expect(screen.getByText("Skills returned: 1")).toBeTruthy();
  expect(
    screen.getByText("Last successful read; current catalog read failed.").closest("details"),
  ).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await act(async () => {});
  expect(screen.getByText("Skills returned: 0")).toBeTruthy();
  expect(screen.queryByText(/Last successful read/)).toBeNull();
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(3);
});

test.each(["api", "context"] as const)(
  "compact %s replacement ignores an obsolete catalog",
  async (boundary) => {
    const old = deferred<NativeCatalog>();
    const client = api({ catalog: vi.fn(() => old.promise) });
    const view = render(
      <SkillsSummary api={client} contextKey="workspace:1" compact onOpen={vi.fn()} />,
    );
    await act(async () => {});
    const signal = vi.mocked(client.catalog).mock.calls[0]?.[1];
    const fresh = boundary === "api" ? api() : client;
    if (boundary === "context") vi.mocked(client.catalog).mockResolvedValueOnce(catalog());
    view.rerender(
      <SkillsSummary
        api={fresh}
        contextKey={boundary === "api" ? "workspace:1" : "workspace:2"}
        compact
        onOpen={vi.fn()}
      />,
    );
    await act(async () => {});
    expect(signal?.aborted).toBe(true);
    expect(screen.getByText("Skills returned: 0")).toBeTruthy();
    await act(async () => old.resolve(catalog(workspace, { assets: [asset("skill", workspace)] })));
    expect(screen.getByText("Skills returned: 0")).toBeTruthy();
    expect(screen.queryByText("Skills returned: 1")).toBeNull();
  },
);

test("compact footer previews three Skills and browses all returned exact refs without another read", async () => {
  const items = [
    { name: "Same", relative_id: "skills/one/SKILL.md", description: "First" },
    { name: "Same", relative_id: "skills/two/SKILL.md", description: "Second" },
    { name: "Third", relative_id: "skills/three/SKILL.md", description: null },
    {
      name: "Fourth",
      relative_id: "skills/four/SKILL.md",
      description: "<img src=x onerror=alert(1)> Fourth description",
    },
  ];
  const client = api({
    catalog: vi.fn(async () =>
      catalog(workspace, {
        partial: true,
        assets: items.map((item) => ({ ...asset("skill", workspace), ...item })),
      }),
    ),
  });
  const activity = <p>Current skill activity</p>;
  const view = render(
    <SkillsSummary api={client} contextKey="workspace:1" compact activity={activity} />,
  );
  await act(async () => {});
  expect(view.container.querySelectorAll(".skills-preview-row")).toHaveLength(3);
  expect(view.container.querySelector(".skills-preview-row")?.getAttribute("title")).toBe("Same");
  expect(screen.getByText("Current skill activity")).toBeTruthy();
  expect(view.container.querySelector(".rail-section-body > details > summary")?.textContent).toBe(
    "Skills details",
  );
  const browse = view.container.querySelector<HTMLDetailsElement>(".skills-browser");
  expect(browse?.querySelector("summary")?.textContent).toBe("Browse Skills (4)");
  if (!browse) throw new Error("Missing Skills browser disclosure");
  fireEvent.click(browse);
  const input = screen.getByRole("searchbox", { name: "Search Skills" });
  expect(view.container.querySelectorAll(".skills-browser-row")).toHaveLength(4);
  expect(screen.getByText("Skills shown: 4 of 4 returned")).toBeTruthy();
  expect(screen.getByText("Ref: skills/one/SKILL.md")).toBeTruthy();
  expect(screen.getByText("Ref: skills/two/SKILL.md")).toBeTruthy();
  expect(view.container.querySelector("img")).toBeNull();
  expect(screen.getByText("<img src=x onerror=alert(1)> Fourth description")).toBeTruthy();
  fireEvent.change(input, { target: { value: "Fourth description" } });
  expect(view.container.querySelectorAll(".skills-browser-row")).toHaveLength(1);
  expect(screen.getByText("Skills shown: 1 of 4 returned")).toBeTruthy();
  fireEvent.change(input, { target: { value: "no match" } });
  expect(screen.getByText("Skills shown: 0 of 4 returned")).toBeTruthy();
  expect(screen.getByText("No returned Skills match this search.")).toBeTruthy();
  expect(view.container.textContent).toContain("Catalog coverage: partial");
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
});

test.each([false, true])(
  "missing Skills opener preserves known data with compact=%s",
  async (compact) => {
    const client = api({
      catalog: vi.fn(async () => catalog(workspace, { assets: [asset("skill", workspace)] })),
    });
    render(<SkillsSummary api={client} contextKey="workspace:1" compact={compact} />);
    await act(async () => {});
    expect(screen.getByText("Skills returned: 1")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Open Skills" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.getByText("Skills browser unavailable.")).toBeTruthy();
    expect(client.sources).toHaveBeenCalledTimes(1);
    expect(client.catalog).toHaveBeenCalledTimes(1);
  },
);

test("changing only presentation preserves the same read and the full default source placement", async () => {
  const client = api();
  const props = { api: client, contextKey: "workspace:1", onOpen: vi.fn() };
  const view = render(<SkillsSummary {...props} />);
  await act(async () => {});
  expect(screen.getByText("Workspace source: workspace:a").closest("details")).toBeNull();
  expect(screen.queryByText("Skills details")).toBeNull();
  view.rerender(<SkillsSummary {...props} compact />);
  expect(screen.getByText("Workspace source: workspace:a").closest("details")).toBeTruthy();
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
});

test("a Skills portal uses the same catalog and exposes literal names and exact identities", async () => {
  const target = document.createElement("div");
  const host = document.createElement("div");
  document.body.append(host);
  const first = {
    ...asset("skill", workspace),
    name: "SKILL",
    description: "<b>A description</b>",
  };
  const second = { ...first, relative_id: "skills/alias/SKILL.md" };
  const client = api({
    catalog: vi.fn(async () =>
      catalog(workspace, { assets: [asset("agent", workspace), first, second], partial: true }),
    ),
  });
  const onOpen = vi.fn();
  const view = render(
    <>
      <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} onOpen={onOpen} />
      <PanelHost
        panels={{ skills: <div ref={(node) => node?.append(target)} /> }}
        hosts={{ left: null, right: host }}
      />
    </>,
  );
  await act(async () => {});
  const rows = within(target).getAllByRole("listitem");
  expect(rows).toHaveLength(2);
  expect(host.contains(target)).toBe(true);
  const [firstRow, secondRow] = rows;
  if (!firstRow || !secondRow) throw new Error("Missing returned skills");
  expect(within(firstRow).getByText("SKILL")).toBeTruthy();
  expect(within(firstRow).getByText(first.description)).toBeTruthy();
  expect(within(firstRow).getByText(`Ref: ${first.relative_id}`)).toBeTruthy();
  expect(within(secondRow).getByText(`Ref: ${second.relative_id}`)).toBeTruthy();
  for (const row of rows)
    expect(within(row).getByText(`Source: ${workspace.source_id}`)).toBeTruthy();
  expect(target.querySelector("b")).toBeNull();
  expect(view.container.textContent).toContain("Skills returned: 2");
  expect(view.container.textContent).toContain("Catalog coverage: partial");
  expect(view.container.querySelector("details")).toBeNull();
  expect(within(target).queryByRole("button")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Open Skills" }));
  expect(onOpen).toHaveBeenCalledTimes(1);
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledWith(workspace.source_id, expect.any(AbortSignal));
  view.unmount();
  expect(target.childElementCount).toBe(0);
  expect(host.childElementCount).toBe(0);
  target.remove();
  host.remove();
});

test("real panel movement retains Skills detail focus and expanded state during the same read", async () => {
  const target = document.createElement("div");
  const left = document.createElement("div");
  const right = document.createElement("div");
  document.body.append(left, right);
  const pending = deferred<NativeCatalog>();
  const client = api({
    catalog: vi
      .fn()
      .mockResolvedValueOnce(
        catalog(workspace, {
          assets: [asset("skill", workspace)],
          partial: true,
          omitted: 1,
          partial_reasons: ["entry_limit"],
        }),
      )
      .mockReturnValueOnce(pending.promise),
  });
  const view = render(
    <>
      <SkillsSummary api={client} contextKey="workspace:1" compact detailsHost={target} />
      <PanelHost
        panels={{ skills: <div ref={(node) => node?.append(target)} /> }}
        hosts={{ left, right }}
      />
    </>,
  );
  await act(async () => {});
  const detail = within(target).getByText("Catalog read details").closest("details");
  const summary = within(target).getByText("Catalog read details");
  if (!detail) throw new Error("Missing catalog detail");
  detail.open = true;
  summary.focus();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  const signal = vi.mocked(client.catalog).mock.calls[1]?.[1];
  fireEvent.click(screen.getByRole("button", { name: "Move Skills left" }));
  expect(left.contains(target)).toBe(true);
  expect(within(target).getByText("Catalog read details")).toBe(summary);
  expect(detail.open).toBe(true);
  expect(document.activeElement).toBe(summary);
  expect(signal?.aborted).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Reset all panels from Skills" }));
  expect(right.contains(target)).toBe(true);
  expect(document.activeElement).toBe(summary);
  expect(detail.open).toBe(true);
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(2);
  await act(async () =>
    pending.resolve(
      catalog(workspace, {
        assets: [asset("skill", workspace)],
        partial: true,
        omitted: 2,
        partial_reasons: ["entry_limit"],
      }),
    ),
  );
  expect(detail.open).toBe(true);
  expect(within(target).getByText("Omitted catalog entries: 2")).toBeTruthy();
  expect(client.catalog).toHaveBeenCalledTimes(2);
  view.unmount();
  expect(target.childElementCount).toBe(0);
  expect(left.childElementCount).toBe(0);
  expect(right.childElementCount).toBe(0);
  left.remove();
  right.remove();
});

test.each(["sources", "catalog", "empty", "pending"] as const)(
  "portal %s keeps unknown coverage in the pinned overview",
  async (stage) => {
    const target = document.createElement("div");
    document.body.append(target);
    const client = api(
      stage === "pending"
        ? { sources: vi.fn(() => deferred<NativeSource[]>().promise) }
        : stage === "empty"
          ? { sources: vi.fn(async () => []) }
          : {
              [stage]: vi.fn(async () => {
                throw new Error("synthetic read failure");
              }),
            },
    );
    const view = render(
      <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />,
    );
    await act(async () => {});
    expect(view.container.textContent).toContain("Skills returned: unknown");
    expect(view.container.textContent).not.toContain("Catalog coverage:");
    expect(within(target).queryByRole("list")).toBeNull();
    expect(target.textContent).not.toContain("No skills were returned");
    expect(
      (screen.getByRole("button", { name: "Open Skills" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    if (stage === "sources" || stage === "catalog")
      expect(within(target).getByRole("alert")).toBeTruthy();
    if (stage !== "catalog") expect(client.catalog).not.toHaveBeenCalled();
    view.unmount();
    expect(target.childElementCount).toBe(0);
    target.remove();
  },
);

test.each([false, true])("portal empty catalog keeps partial=%s qualified", async (partial) => {
  const target = document.createElement("div");
  document.body.append(target);
  const client = api({
    catalog: vi.fn(async () =>
      catalog(workspace, {
        partial,
        omitted: partial ? 3 : 0,
        partial_reasons: partial ? ["entry_limit"] : [],
      }),
    ),
  });
  const view = render(<SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />);
  await act(async () => {});
  expect(view.container.textContent).toContain("Skills returned: 0");
  expect(view.container.textContent).toContain(
    `Catalog coverage: ${partial ? "partial" : "complete"}`,
  );
  expect(
    within(target).getByText(
      partial
        ? "No skills were returned; this catalog is partial."
        : "No skills were returned by this complete catalog.",
    ),
  ).toBeTruthy();
  if (partial) {
    expect(within(target).getByText("Omitted catalog entries: 3")).toBeTruthy();
    expect(within(target).getByText("entry_limit")).toBeTruthy();
  }
  expect(within(target).queryByRole("list")).toBeNull();
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
  view.unmount();
  target.remove();
});

test.each(["api", "context"] as const)(
  "portal %s replacement discards both obsolete read outcomes",
  async (boundary) => {
    for (const failure of [false, true]) {
      const target = document.createElement("div");
      document.body.append(target);
      const old = deferred<NativeCatalog>();
      const freshRead = deferred<NativeCatalog>();
      const client = api({ catalog: vi.fn(() => old.promise) });
      const view = render(
        <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />,
      );
      await act(async () => {});
      const signal = vi.mocked(client.catalog).mock.calls[0]?.[1];
      const fresh = boundary === "api" ? api({ catalog: vi.fn(() => freshRead.promise) }) : client;
      if (boundary === "context") vi.mocked(client.catalog).mockReturnValueOnce(freshRead.promise);
      view.rerender(
        <SkillsSummary
          api={fresh}
          contextKey={boundary === "api" ? "workspace:1" : "workspace:2"}
          detailsHost={target}
        />,
      );
      await act(async () => {});
      expect(signal?.aborted).toBe(true);
      expect(view.container.textContent).toContain("Skills returned: unknown");
      expect(within(target).queryByRole("list")).toBeNull();
      const current = { ...asset("skill", workspace), name: "Current name" };
      await act(async () => freshRead.resolve(catalog(workspace, { assets: [current] })));
      const row = within(target).getByRole("listitem");
      await act(async () => {
        if (failure) old.reject(new Error("obsolete catalog failure"));
        else old.resolve(catalog(workspace, { assets: [asset("skill", workspace)] }));
      });
      expect(within(target).getByRole("listitem")).toBe(row);
      expect(within(row).getByText(current.name)).toBeTruthy();
      expect(within(target).queryByText("Example")).toBeNull();
      expect(within(target).queryByRole("alert")).toBeNull();
      expect(vi.mocked(fresh.catalog).mock.calls.length).toBe(boundary === "api" ? 1 : 2);
      view.unmount();
      expect(target.childElementCount).toBe(0);
      target.remove();
    }
  },
);

test("portal Retry keeps last-read details through failure and clears them on success", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const pending = deferred<NativeCatalog>();
  const client = api({
    catalog: vi
      .fn()
      .mockResolvedValueOnce(
        catalog(workspace, {
          assets: [asset("skill", workspace)],
          partial: true,
        }),
      )
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(catalog()),
  });
  const view = render(<SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />);
  await act(async () => {});
  const row = within(target).getByRole("listitem");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(view.container.textContent).toContain(
    "Last successful read; current catalog read is pending.",
  );
  expect(within(target).getByRole("listitem")).toBe(row);
  await act(async () => pending.reject(new Error("synthetic catalog failure")));
  expect(view.container.textContent).toContain(
    "Last successful read; current catalog read failed.",
  );
  expect(within(target).getByRole("listitem")).toBe(row);
  expect(within(target).getByRole("alert")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await act(async () => {});
  expect(view.container.textContent).toContain("Skills returned: 0");
  expect(view.container.textContent).not.toContain("Last successful read");
  expect(within(target).queryByRole("list")).toBeNull();
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(3);
  view.unmount();
  target.remove();
});

test("attaching and replacing only the portal target keeps the read owner and cleans old DOM", async () => {
  const first = document.createElement("div");
  const second = document.createElement("div");
  document.body.append(first, second);
  const pending = deferred<NativeCatalog>();
  const client = api({ catalog: vi.fn(() => pending.promise) });
  const props = { api: client, contextKey: "workspace:1", compact: true };
  const view = render(<SkillsSummary {...props} />);
  await act(async () => {});
  const signal = vi.mocked(client.catalog).mock.calls[0]?.[1];
  view.rerender(<SkillsSummary {...props} detailsHost={first} />);
  expect(within(first).getByText("Loading Skills catalog…")).toBeTruthy();
  view.rerender(<SkillsSummary {...props} detailsHost={second} />);
  expect(first.childElementCount).toBe(0);
  expect(within(second).getByText("Loading Skills catalog…")).toBeTruthy();
  expect(signal?.aborted).toBe(false);
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
  await act(async () =>
    pending.resolve(catalog(workspace, { assets: [asset("skill", workspace)] })),
  );
  expect(within(second).getByRole("listitem")).toBeTruthy();
  view.rerender(<SkillsSummary {...props} detailsHost={null} />);
  expect(second.childElementCount).toBe(0);
  expect(view.container.querySelector("details")).toBeTruthy();
  expect(client.catalog).toHaveBeenCalledTimes(1);
  view.unmount();
  first.remove();
  second.remove();
});

test.each(["sources", "catalog"] as const)(
  "unmount aborts portal %s and ignores both late outcomes",
  async (stage) => {
    for (const failure of [false, true]) {
      const target = document.createElement("div");
      document.body.append(target);
      const sourceRead = deferred<NativeSource[]>();
      const catalogRead = deferred<NativeCatalog>();
      const client = api(
        stage === "sources"
          ? { sources: vi.fn(() => sourceRead.promise) }
          : { catalog: vi.fn(() => catalogRead.promise) },
      );
      const view = render(
        <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />,
      );
      await act(async () => {});
      const signal =
        stage === "sources"
          ? vi.mocked(client.sources).mock.calls[0]?.[0]
          : vi.mocked(client.catalog).mock.calls[0]?.[1];
      view.unmount();
      expect(signal?.aborted).toBe(true);
      expect(target.childElementCount).toBe(0);
      await act(async () => {
        if (stage === "sources") {
          if (failure) sourceRead.reject(new Error("late sources failure"));
          else sourceRead.resolve([workspace]);
        } else if (failure) catalogRead.reject(new Error("late catalog failure"));
        else catalogRead.resolve(catalog(workspace, { assets: [asset("skill", workspace)] }));
      });
      expect(target.childElementCount).toBe(0);
      expect(client.catalog).toHaveBeenCalledTimes(stage === "sources" ? 0 : 1);
      target.remove();
    }
  },
);

test.each([
  { field: "name", query: "日本語", refs: ["skills/日本/SKILL.md"] },
  { field: "description", query: "灯り", refs: ["skills/日本/SKILL.md"] },
  { field: "relative_id", query: "skills/other", refs: ["skills/other/SKILL.md"] },
  {
    field: "source_id",
    query: workspace.source_id,
    refs: ["skills/日本/SKILL.md", "skills/other/SKILL.md"],
  },
])(
  "Skills search filters only the current catalog's $field without another read",
  async ({ query, refs }) => {
    const target = document.createElement("div");
    document.body.append(target);
    const client = api({
      catalog: vi.fn(async () =>
        catalog(workspace, {
          assets: [
            {
              ...asset("skill", workspace),
              name: "日本語 SKILL",
              description: "灯り detail",
              relative_id: "skills/日本/SKILL.md",
            },
            { ...asset("skill", workspace), name: "Other", relative_id: "skills/other/SKILL.md" },
            asset("agent", workspace),
          ],
          partial: true,
          partial_reasons: ["deadline"],
        }),
      ),
    });
    const view = render(
      <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />,
    );
    await act(async () => {});
    const input = within(target).getByRole("searchbox", { name: "Search Skills" });
    fireEvent.change(input, { target: { value: query } });
    const rows = within(target).getAllByRole("listitem");
    expect(rows.map((row) => within(row).getByText(/^Ref:/).textContent)).toEqual(
      refs.map((ref) => `Ref: ${ref}`),
    );
    expect(view.container.textContent).toContain("Skills returned: 2");
    expect(view.container.textContent).toContain("Catalog coverage: partial");
    expect(within(target).getByText(`Skills shown: ${refs.length} of 2 returned`)).toBeTruthy();
    for (const row of rows)
      expect(within(row).getByText(`Source: ${workspace.source_id}`)).toBeTruthy();
    expect(within(target).queryByRole("button")).toBeNull();
    expect(client.sources).toHaveBeenCalledTimes(1);
    expect(client.catalog).toHaveBeenCalledTimes(1);
    expect(client.catalog).toHaveBeenCalledWith(workspace.source_id, expect.any(AbortSignal));
    view.unmount();
    target.remove();
  },
);

test.each([false, true])(
  "Skills query, caret and exact input survive real move/reset/Retry failure=%s",
  async (failure) => {
    const target = document.createElement("div");
    const left = document.createElement("div");
    const right = document.createElement("div");
    document.body.append(left, right);
    const pending = deferred<NativeCatalog>();
    const current = catalog(workspace, {
      assets: [
        { ...asset("skill", workspace), name: "日本語の灯り", relative_id: "skills/日本/SKILL.md" },
        { ...asset("skill", workspace), name: "Other", relative_id: "skills/other/SKILL.md" },
      ],
      partial: true,
    });
    const client = api({
      catalog: vi.fn().mockResolvedValueOnce(current).mockReturnValueOnce(pending.promise),
    });
    const view = render(
      <>
        <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />
        <PanelHost
          panels={{ skills: <div ref={(node) => node?.append(target)} /> }}
          hosts={{ left, right }}
        />
      </>,
    );
    await act(async () => {});
    const input = within(target).getByRole("searchbox", { name: "Search Skills" });
    if (!(input instanceof HTMLInputElement)) throw new Error("Missing native search input");
    fireEvent.change(input, { target: { value: "日本語" } });
    input.focus();
    input.setSelectionRange(1, 2);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    const signal = vi.mocked(client.catalog).mock.calls[1]?.[1];
    for (const name of ["Move Skills left", "Reset all panels from Skills"]) {
      fireEvent.click(screen.getByRole("button", { name }));
      expect(within(target).getByRole("searchbox", { name: "Search Skills" })).toBe(input);
      expect(input.value).toBe("日本語");
      expect(input.selectionStart).toBe(1);
      expect(input.selectionEnd).toBe(2);
      expect(document.activeElement).toBe(input);
      expect(signal?.aborted).toBe(false);
      expect(client.catalog).toHaveBeenCalledTimes(2);
    }
    expect(right.contains(target)).toBe(true);
    await act(async () => {
      if (failure) pending.reject(new Error("synthetic Retry failure"));
      else pending.resolve(current);
    });
    expect(within(target).getByRole("searchbox", { name: "Search Skills" })).toBe(input);
    expect(input.value).toBe("日本語");
    expect(input.selectionStart).toBe(1);
    expect(input.selectionEnd).toBe(2);
    expect(document.activeElement).toBe(input);
    expect(within(target).getByText("Ref: skills/日本/SKILL.md")).toBeTruthy();
    expect(within(target).getByText("Skills shown: 1 of 2 returned")).toBeTruthy();
    expect(view.container.textContent).toContain("Skills returned: 2");
    if (failure)
      expect(view.container.textContent).toContain(
        "Last successful read; current catalog read failed.",
      );
    expect(client.sources).toHaveBeenCalledTimes(1);
    expect(client.catalog).toHaveBeenCalledTimes(2);
    view.unmount();
    expect(target.childElementCount).toBe(0);
    left.remove();
    right.remove();
  },
);

test("clearing a Skills no-match search restores exact rows without changing catalog coverage", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const client = api({
    catalog: vi.fn(async () =>
      catalog(workspace, {
        assets: [asset("skill", workspace)],
        partial: true,
        partial_reasons: ["deadline"],
      }),
    ),
  });
  const view = render(<SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />);
  await act(async () => {});
  const input = within(target).getByRole("searchbox", { name: "Search Skills" });
  fireEvent.change(input, { target: { value: "no such skill" } });
  expect(within(target).queryByRole("listitem")).toBeNull();
  expect(within(target).getByText("Skills shown: 0 of 1 returned")).toBeTruthy();
  expect(within(target).getByText("No returned Skills match this search.")).toBeTruthy();
  expect(view.container.textContent).toContain("Skills returned: 1");
  expect(view.container.textContent).toContain("Catalog coverage: partial");
  fireEvent.change(input, { target: { value: "" } });
  expect(within(target).getByText("Ref: skills/example/SKILL.md")).toBeTruthy();
  expect(within(target).getByText("Skills shown: 1 of 1 returned")).toBeTruthy();
  expect(within(target).queryByText("No returned Skills match this search.")).toBeNull();
  expect(client.sources).toHaveBeenCalledTimes(1);
  expect(client.catalog).toHaveBeenCalledTimes(1);
  view.unmount();
  target.remove();
});

test.each(["sources", "catalog", "empty", "pending"] as const)(
  "search of an unknown %s read keeps filtered count unknown",
  async (stage) => {
    const target = document.createElement("div");
    document.body.append(target);
    const client = api(
      stage === "pending"
        ? { sources: vi.fn(() => deferred<NativeSource[]>().promise) }
        : stage === "empty"
          ? { sources: vi.fn(async () => []) }
          : {
              [stage]: vi.fn(async () => {
                throw new Error("synthetic read failure");
              }),
            },
    );
    const view = render(
      <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />,
    );
    await act(async () => {});
    fireEvent.change(within(target).getByRole("searchbox", { name: "Search Skills" }), {
      target: { value: "日本語" },
    });
    expect(within(target).getByText("Skills shown: unknown")).toBeTruthy();
    expect(target.textContent).not.toContain("Skills shown: 0");
    expect(within(target).queryByText("No returned Skills match this search.")).toBeNull();
    expect(view.container.textContent).toContain("Skills returned: unknown");
    expect(view.container.textContent).not.toContain("Catalog coverage:");
    expect(client.sources).toHaveBeenCalledTimes(1);
    expect(client.catalog).toHaveBeenCalledTimes(stage === "catalog" ? 1 : 0);
    view.unmount();
    target.remove();
  },
);

test.each([false, true])(
  "search of a known empty catalog preserves partial=%s",
  async (partial) => {
    const target = document.createElement("div");
    document.body.append(target);
    const client = api({ catalog: vi.fn(async () => catalog(workspace, { partial })) });
    const view = render(
      <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />,
    );
    await act(async () => {});
    fireEvent.change(within(target).getByRole("searchbox", { name: "Search Skills" }), {
      target: { value: "日本語" },
    });
    expect(within(target).getByText("Skills shown: 0 of 0 returned")).toBeTruthy();
    expect(within(target).queryByText("No returned Skills match this search.")).toBeNull();
    expect(view.container.textContent).toContain("Skills returned: 0");
    expect(view.container.textContent).toContain(
      `Catalog coverage: ${partial ? "partial" : "complete"}`,
    );
    expect(client.sources).toHaveBeenCalledTimes(1);
    expect(client.catalog).toHaveBeenCalledTimes(1);
    view.unmount();
    target.remove();
  },
);

test.each(["api", "context"] as const)(
  "the search query stays local while %s discards both obsolete read outcomes",
  async (boundary) => {
    for (const failure of [false, true]) {
      const target = document.createElement("div");
      document.body.append(target);
      const old = deferred<NativeCatalog>();
      const freshRead = deferred<NativeCatalog>();
      const client = api({ catalog: vi.fn(() => old.promise) });
      const view = render(
        <SkillsSummary api={client} contextKey="workspace:1" detailsHost={target} />,
      );
      await act(async () => {});
      const input = within(target).getByRole("searchbox", { name: "Search Skills" });
      if (!(input instanceof HTMLInputElement)) throw new Error("Missing native search input");
      fireEvent.change(input, { target: { value: "日本語" } });
      const oldSignal = vi.mocked(client.catalog).mock.calls[0]?.[1];
      const fresh = boundary === "api" ? api({ catalog: vi.fn(() => freshRead.promise) }) : client;
      if (boundary === "context") vi.mocked(client.catalog).mockReturnValueOnce(freshRead.promise);
      view.rerender(
        <SkillsSummary
          api={fresh}
          contextKey={boundary === "api" ? "workspace:1" : "workspace:2"}
          detailsHost={target}
        />,
      );
      await act(async () => {});
      expect(oldSignal?.aborted).toBe(true);
      expect(within(target).getByRole("searchbox", { name: "Search Skills" })).toBe(input);
      expect(input.value).toBe("日本語");
      expect(within(target).getByText("Skills shown: unknown")).toBeTruthy();
      const current = {
        ...asset("skill", workspace),
        name: "日本語 Current",
        relative_id: "skills/current/SKILL.md",
      };
      await act(async () =>
        freshRead.resolve(
          catalog(workspace, {
            assets: [
              current,
              {
                ...asset("skill", workspace),
                name: "Other",
                relative_id: "skills/other/SKILL.md",
              },
            ],
          }),
        ),
      );
      const row = within(target).getByRole("listitem");
      await act(async () => {
        if (failure) old.reject(new Error("obsolete failure"));
        else
          old.resolve(
            catalog(workspace, { assets: [{ ...asset("skill", workspace), name: "日本語 Old" }] }),
          );
      });
      expect(within(target).getByRole("listitem")).toBe(row);
      expect(within(row).getByText("Ref: skills/current/SKILL.md")).toBeTruthy();
      expect(within(row).getByText(current.name)).toBeTruthy();
      expect(within(target).queryByText("日本語 Old")).toBeNull();
      expect(within(target).queryByRole("alert")).toBeNull();
      expect(within(target).getByText("Skills shown: 1 of 2 returned")).toBeTruthy();
      expect(view.container.textContent).toContain("Skills returned: 2");
      expect(fresh.catalog).toHaveBeenCalledTimes(boundary === "api" ? 1 : 2);
      view.unmount();
      target.remove();
    }
  },
);
