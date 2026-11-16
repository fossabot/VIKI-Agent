import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { InventoryApi } from "../../clients/conversations/inventoryApi";
import type { NativeAssetsApi } from "../../clients/profiles/nativeAssetsApi";
import {
  blankProfileDocument,
  type ProfileApi,
  type ProfileDefinition,
} from "../../clients/profiles/profileApi";
import type {
  PreparedProfilePreview,
  ProfileLaunchApi,
} from "../../clients/profiles/profileLaunchApi";
import { Shell } from "../../components/Shell/Shell";
import { TopBar } from "../../components/TopBar/TopBar";
import { fakeDurable } from "../../testDurable";
import {
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

const PROFILE = "00000000-0000-4000-8000-000000000001";
const AT = "2026-10-12T00:00:00.000Z";

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

function profileApi(): ProfileApi {
  const document = blankProfileDocument();
  document.identity.name = "Synthetic profile";
  document.tools.mcp_configuration = {
    servers: [
      {
        name: "Library",
        transport: "stdio",
        command: "synthetic",
        args: [],
        url: null,
        note: null,
      },
    ],
  };
  const source: ProfileDefinition = {
    profile_id: PROFILE,
    revision: 1,
    current_revision: 1,
    state_revision: 1,
    created_at: AT,
    updated_at: AT,
    archived: false,
    document,
  };
  return {
    list: vi.fn(async () => [
      {
        ...source,
        identity: document.identity,
        runtime: document.runtime,
        provenance: document.provenance,
      },
    ]),
    get: vi.fn(async () => source),
    create: vi.fn(async (id) => ({ profile_id: id, revision: 1, state_revision: 1 })),
    revise: vi.fn(),
    archive: vi.fn(),
    duplicate: vi.fn(),
    compare: vi.fn(),
  };
}

const nativeApi: NativeAssetsApi = { sources: async () => [], catalog: vi.fn() };
function inventoryApi(): InventoryApi {
  return { get: vi.fn() };
}

async function mount(props: Partial<Parameters<typeof App>[0]> = {}) {
  const { viki, socket } = fakeViki();
  const shell = fakeShellApi({
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspaceFixture()]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(
      <App viki={viki} rest={fakeRest()} durable={fakeDurable()} shell={shell} {...props} />,
    );
    socket.open();
  });
  return { ...result, viki, socket };
}

async function click(element: HTMLElement) {
  await act(async () => {
    fireEvent.click(element);
  });
}
async function pickProfile() {
  await act(async () => {
    fireEvent.change(screen.getByRole("combobox", { name: "Saved app profile" }), {
      target: { value: `${PROFILE}:1` },
    });
  });
}

test("available Capabilities navigation renders five tabs and preserves the existing shell", async () => {
  const inventory = inventoryApi();
  const { container } = await mount({ inventoryApi: inventory });
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Capabilities" },
    ),
  );
  const hub = screen.getByRole("region", { name: "Capabilities" });
  expect(
    within(hub)
      .getAllByRole("tab")
      .map((tab) => tab.textContent),
  ).toEqual(["Skills", "Subagents", "Tools", "Connectors", "Plugins"]);
  expect(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" }))
      .getByRole("button", { name: "Capabilities" })
      .getAttribute("aria-current"),
  ).toBe("page");
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Capabilities" }));
  expect(container.querySelector('[data-region="left-rail"]')).not.toBeNull();
  expect(container.querySelector('[data-region="conversation"]')).toBeNull();
  expect(
    screen.getByText("Native skill discovery is unavailable in this environment."),
  ).toBeTruthy();
  expect(inventory.get).not.toHaveBeenCalled();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();
});

test("restoration requires a capability client and the sidebar includes the hub", async () => {
  sessionStorage.setItem("viki.view", JSON.stringify({ view: "capabilities" }));
  await mount({ inventoryApi: inventoryApi() });
  expect(screen.getByRole("region", { name: "Capabilities" })).toBeTruthy();
  const navigation = within(screen.getByRole("navigation", { name: "Navigation and sessions" }));
  expect(navigation.getByRole("button", { name: "Capabilities" })).toBeTruthy();
  await click(navigation.getByRole("button", { name: "Chat" }));
  expect(screen.queryByRole("region", { name: "Capabilities" })).toBeNull();
  cleanup();
  sessionStorage.setItem("viki.view", JSON.stringify({ view: "capabilities" }));
  const { container } = await mount();
  expect(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).queryByRole(
      "button",
      { name: "Capabilities" },
    ),
  ).toBeNull();
  expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();
});

test("connector edits survive category switches and refuse management navigation until admitted", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await mount({ profileApi: profileApi() });
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Capabilities" },
    ),
  );
  await click(screen.getByRole("tab", { name: "Connectors" }));
  await pickProfile();
  await click(screen.getByRole("checkbox", { name: "Include Library in next session" }));
  await click(screen.getByRole("tab", { name: "Tools" }));
  await click(screen.getByRole("tab", { name: "Connectors" }));
  expect(
    (screen.getByRole("checkbox", { name: "Include Library in next session" }) as HTMLInputElement)
      .checked,
  ).toBe(false);
  await click(screen.getByRole("button", { name: "Manage profiles" }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("region", { name: "Capabilities" })).toBeTruthy();
  confirm.mockReturnValue(true);
  await click(screen.getByRole("button", { name: "Manage profiles" }));
  expect(screen.getByRole("tab", { name: "Profiles" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.queryByRole("region", { name: "Capabilities" })).toBeNull();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Capabilities" },
    ),
  );
  await click(screen.getByRole("tab", { name: "Connectors" }));
  await pickProfile();
  await click(screen.getByRole("checkbox", { name: "Include Library in next session" }));
  confirm.mockReturnValue(false);
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  expect(screen.getByRole("region", { name: "Capabilities" })).toBeTruthy();
});

test("native management opens Subagents inside Capabilities", async () => {
  await mount({ profileApi: profileApi(), nativeAssetsApi: nativeApi });
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Capabilities" },
    ),
  );
  await click(screen.getByRole("button", { name: "Manage native capabilities" }));
  expect(screen.getByRole("tab", { name: "Subagents" }).getAttribute("aria-selected")).toBe("true");
});

test("prepared connector copy opens bound from its first frame without discarding saved inclusion", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  let preview!: PreparedProfilePreview;
  let inspected = false;
  const launch: ProfileLaunchApi = {
    capabilities: vi.fn(),
    models: vi.fn(),
    prepare: vi.fn(async (profile, _command, session, revision, workspace) => {
      preview = {
        launchable: true,
        fields: [],
        blockers: [],
        warnings: [],
        prefs: { mode: "default", effort: "high", model: null },
        session_id: session,
        profile_id: profile,
        profile_revision: revision,
        generation: null,
        workspace_id: workspace,
        workspace_revision: 1,
        policy: "ordinary",
        policy_revision: 1,
        settings_revision: 1,
        runtime: "claude",
        capability_key: "a".repeat(64),
        started: false,
        draft: { key: session, revision: 1, text: "", saved_at: null, attachments: [] },
        identity: {
          name: "Prepared identity",
          avatar_asset_id: null,
          accent: "orange",
          shape: "rounded",
          profile_revision: revision,
        },
      };
      return preview;
    }),
    inspect: vi.fn(() => {
      if (inspected) return new Promise<PreparedProfilePreview>(() => {});
      inspected = true;
      return Promise.resolve(preview);
    }),
  };
  const { container, socket } = await mount({ profileApi: profileApi(), profileLaunchApi: launch });
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Capabilities" },
    ),
  );
  await click(screen.getByRole("tab", { name: "Connectors" }));
  await pickProfile();
  await click(screen.getByRole("checkbox", { name: "Include Library in next session" }));
  await click(screen.getByRole("button", { name: "Review next session" }));
  await click(screen.getByRole("button", { name: "Save new profile copy and prepare draft" }));
  expect(confirm).not.toHaveBeenCalled();
  expect(container.querySelector('[data-region="conversation-header"]')?.textContent).toContain(
    "Prepared identity · r1",
  );
  expect(container.querySelector(".face-neutral")).toBeNull();
  expect(socket.sent.some((frame) => frame.type === "turn.send")).toBe(false);
});

test("optional view sidebar uses docked resize controls or the same narrow drawer", () => {
  const props = {
    topBar: null,
    leftRail: null,
    header: null,
    messages: null,
    composer: null,
    rightRail: null,
    statusBar: null,
    view: <h1>Hub</h1>,
  };
  const { container, rerender } = render(
    <Shell {...props} viewSidebar={<span>Session sidebar</span>} />,
  );
  expect(screen.getByRole("separator", { name: "Resize left rail" })).toBeTruthy();
  expect(screen.getByText("Session sidebar")).toBeTruthy();
  rerender(
    <Shell
      {...props}
      viewSidebar={<span>Session sidebar</span>}
      layout={{ leftDocked: false, rightDocked: false }}
      rails={{ drawer: "left", onClose: vi.fn() }}
    />,
  );
  expect(screen.getByRole("dialog", { name: "Sessions and agents" })).toBeTruthy();
  expect(screen.queryByRole("separator")).toBeNull();
  expect(container.querySelector('[data-region="conversation"]')).toBeNull();
});

test("optional TopBar hub stays in the keyboard tab order without changing absent-client tabs", () => {
  const onCapabilities = vi.fn();
  const { rerender } = render(
    <TopBar
      onChat={vi.fn()}
      onCapabilities={onCapabilities}
      onNewSession={vi.fn()}
      view="capabilities"
    />,
  );
  const hub = screen.getByRole("tab", { name: "Capabilities" });
  expect(hub.tabIndex).toBe(0);
  hub.focus();
  fireEvent.keyDown(hub, { key: "ArrowLeft" });
  expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Chat" }));
  fireEvent.click(hub);
  expect(onCapabilities).toHaveBeenCalledOnce();
  rerender(<TopBar onNewSession={vi.fn()} />);
  expect(screen.queryByRole("tab", { name: "Capabilities" })).toBeNull();
});
