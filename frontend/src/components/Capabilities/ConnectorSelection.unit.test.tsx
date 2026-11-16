import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  blankProfileDocument,
  type ProfileApi,
  type ProfileDefinition,
  type ProfileSummary,
} from "../../clients/profiles/profileApi";
import type {
  PreparedProfilePreview,
  ProfileLaunchApi,
} from "../../clients/profiles/profileLaunchApi";
import { hasUnsavedWork } from "../../state/unsavedWork";
import { ConnectorSelection } from "./ConnectorSelection";

const PROFILE = "00000000-0000-4000-8000-000000000001";
const WORKSPACE = "00000000-0000-4000-8000-000000000002";
const NOW = "2026-10-12T00:00:00.000Z";

function definition(): ProfileDefinition {
  const document = blankProfileDocument();
  document.identity.name = "Synthetic profile";
  document.tools.mcp_configuration = {
    servers: [
      {
        name: "Library",
        transport: "stdio",
        command: "synthetic",
        args: ["private-value"],
        url: null,
        note: null,
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
    revision: 3,
    current_revision: 3,
    state_revision: 1,
    archived: false,
    document,
    created_at: NOW,
    updated_at: NOW,
  };
}

function api(): ProfileApi {
  const source = definition();
  const summary: ProfileSummary = {
    ...source,
    identity: source.document.identity,
    runtime: source.document.runtime,
    provenance: source.document.provenance,
  };
  return {
    list: vi.fn(async () => [summary]),
    get: vi.fn(async () => source),
    create: vi.fn(async (id) => ({ profile_id: id, revision: 1, state_revision: 1 })),
    revise: vi.fn(),
    archive: vi.fn(),
    compare: vi.fn(),
    duplicate: vi.fn(),
  };
}

function launchApi(): ProfileLaunchApi {
  let preview: PreparedProfilePreview;
  return {
    capabilities: vi.fn(),
    models: vi.fn(),
    prepare: vi.fn(async (profileId, _command, id, revision) => {
      preview = {
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
        draft: { key: id, revision: 1, text: "", saved_at: null, attachments: [] },
        started: false,
      };
      return preview;
    }),
    inspect: vi.fn(async () => preview),
  };
}

async function selectProfile(): Promise<void> {
  await act(async () => Promise.resolve());
  fireEvent.change(screen.getByRole("combobox", { name: "Saved app profile" }), {
    target: { value: `${PROFILE}:3` },
  });
  await act(async () => Promise.resolve());
}

afterEach(cleanup);

it("loads one selected exact revision and shows actual connector inclusion without configuration values", async () => {
  const profiles = api(),
    launch = launchApi();
  render(<ConnectorSelection profileApi={profiles} launchApi={launch} workspaceId={WORKSPACE} />);
  await selectProfile();
  expect(profiles.get).toHaveBeenCalledOnce();
  expect(profiles.get).toHaveBeenCalledWith(PROFILE, 3, expect.any(AbortSignal));
  expect(screen.getByText(PROFILE)).toBeTruthy();
  expect(
    (screen.getByRole("checkbox", { name: "Include Library in next session" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  expect(screen.queryByText("private-value")).toBeNull();
  expect(screen.queryByText("https://example.invalid/mcp")).toBeNull();
  expect(launch.prepare).not.toHaveBeenCalled();
});

it("reviews an exact subset, confirms a new copy and clears unsaved state after eligible preparation", async () => {
  const profiles = api(),
    launch = launchApi(),
    onPrepared = vi.fn(() => hasUnsavedWork());
  render(
    <ConnectorSelection
      profileApi={profiles}
      launchApi={launch}
      workspaceId={WORKSPACE}
      onPrepared={onPrepared}
    />,
  );
  await selectProfile();
  fireEvent.click(screen.getByRole("checkbox", { name: "Include Calendar in next session" }));
  expect(hasUnsavedWork()).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Review next session" }));
  expect(screen.getByRole("region", { name: "Next session review" }).textContent).toContain(
    "Library",
  );
  expect(profiles.create).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save new profile copy and prepare draft" }));
  await act(async () => Promise.resolve());
  expect(profiles.create).toHaveBeenCalledOnce();
  expect(profiles.revise).not.toHaveBeenCalled();
  expect(onPrepared).toHaveBeenCalledOnce();
  expect(onPrepared.mock.results[0]?.value).toBe(false);
  expect(hasUnsavedWork()).toBe(false);
});

it("keeps a blocked preview and selection visible without successful handoff", async () => {
  const profiles = api(),
    launch = launchApi(),
    onPrepared = vi.fn();
  vi.mocked(launch.prepare).mockResolvedValue({
    launchable: false,
    fields: [
      {
        field: "memory.mode",
        status: "unsupported",
        preview_class: "unsupported",
        effective_value: "isolated",
        source: "profile",
        reason: "memory_isolation_unproven",
      },
    ],
    blockers: ["memory.mode", "future_blocker"],
    profile_id: PROFILE,
    profile_revision: 3,
  });
  const view = render(
    <ConnectorSelection
      profileApi={profiles}
      launchApi={launch}
      workspaceId={WORKSPACE}
      onPrepared={onPrepared}
    />,
  );
  await selectProfile();
  fireEvent.click(screen.getByRole("button", { name: "Review next session" }));
  fireEvent.click(screen.getByRole("button", { name: "Prepare draft from saved revision" }));
  await act(async () => Promise.resolve());
  expect(screen.getByText("Draft is blocked.")).toBeTruthy();
  expect(
    screen.getAllByText(/custom memory setting has not been verified as applied/),
  ).toHaveLength(2);
  expect(screen.getByText(/future_blocker/)).toBeTruthy();
  view.rerender(
    <ConnectorSelection
      profileApi={profiles}
      launchApi={launch}
      workspaceId={WORKSPACE}
      onPrepared={onPrepared}
    />,
  );
  expect(
    screen.getAllByText(/custom memory setting has not been verified as applied/),
  ).toHaveLength(2);
  expect(onPrepared).not.toHaveBeenCalled();
  expect(
    (screen.getByRole("checkbox", { name: "Include Library in next session" }) as HTMLInputElement)
      .checked,
  ).toBe(true);
});

it("uses each matching preview reason in the blocker list", async () => {
  const profiles = api();
  const launch = launchApi();
  vi.mocked(launch.prepare).mockResolvedValue({
    launchable: false,
    fields: [
      {
        field: "workspace.allowed_workspace_ids",
        status: "unsupported",
        preview_class: "unsupported",
        effective_value: null,
        source: "profile",
        reason: "workspace_not_allowed",
      },
      {
        field: "tools.mcp_configuration",
        status: "unsupported",
        preview_class: "unsupported",
        effective_value: null,
        source: "profile",
        reason: "mcp_policy_blocked",
      },
    ],
    blockers: ["workspace.allowed_workspace_ids", "tools.mcp_configuration"],
    profile_id: PROFILE,
    profile_revision: 3,
  });
  render(<ConnectorSelection profileApi={profiles} launchApi={launch} workspaceId={WORKSPACE} />);
  await selectProfile();
  fireEvent.click(screen.getByRole("button", { name: "Review next session" }));
  fireEvent.click(screen.getByRole("button", { name: "Prepare draft from saved revision" }));
  await act(async () => Promise.resolve());
  expect(screen.getByRole("list").textContent).toContain(
    "This workspace is not allowed by the profile.",
  );
  expect(screen.getByRole("list").textContent).toContain(
    "This profile's MCP servers cannot run in this workspace.",
  );
});

it("retains inclusion after a create failure and supports explicit retry", async () => {
  const profiles = api(),
    launch = launchApi();
  vi.mocked(profiles.create).mockRejectedValueOnce(new Error("Synthetic failure"));
  render(<ConnectorSelection profileApi={profiles} launchApi={launch} workspaceId={WORKSPACE} />);
  await selectProfile();
  fireEvent.click(screen.getByRole("checkbox", { name: "Include Library in next session" }));
  fireEvent.click(screen.getByRole("button", { name: "Review next session" }));
  fireEvent.click(screen.getByRole("button", { name: "Save new profile copy and prepare draft" }));
  await act(async () => Promise.resolve());
  expect(screen.getByRole("alert").textContent).toMatch(/selection is retained/);
  expect(
    (screen.getByRole("checkbox", { name: "Include Library in next session" }) as HTMLInputElement)
      .checked,
  ).toBe(false);
  expect(profiles.create).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "Save new profile copy and prepare draft" }));
  await act(async () => Promise.resolve());
  expect(profiles.create).toHaveBeenCalledTimes(2);
});

it("ignores a late definition when profile API is replaced and aborts on unmount", async () => {
  const profiles = api(),
    next = api();
  let resolve!: (value: ProfileDefinition) => void;
  vi.mocked(profiles.get).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { rerender, unmount } = render(<ConnectorSelection profileApi={profiles} />);
  await selectProfile();
  const signal = vi.mocked(profiles.get).mock.calls[0]?.[2];
  rerender(<ConnectorSelection profileApi={next} />);
  await act(async () => {
    resolve(definition());
  });
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByRole("checkbox")).toBeNull();
  await selectProfile();
  const nextSignal = vi.mocked(next.get).mock.calls[0]?.[2];
  unmount();
  expect(nextSignal?.aborted).toBe(true);
  expect(hasUnsavedWork()).toBe(false);
});

it("does not invent inclusion switches for unknown MCP shapes or prepare without workspace", async () => {
  const profiles = api();
  const source = definition();
  source.document.tools.mcp_configuration = { future_format: true };
  vi.mocked(profiles.get).mockResolvedValue(source);
  render(<ConnectorSelection profileApi={profiles} launchApi={launchApi()} />);
  await selectProfile();
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.getByText(/cannot be selected here/)).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "Review next session" }) as HTMLButtonElement).disabled,
  ).toBe(true);
});
