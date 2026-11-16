/**
 * Leaving the Vault or Agents view never drops unsaved work. Every App
 * navigation away from a dirty view asks first, a cancelled prompt keeps the view and the edit,
 * Escape typed inside a field of a dirty view is not a request to leave, and `beforeunload` is
 * blocked while the edit is held. The views' own `dirty` rules are pinned beside each view.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { SessionMetadataApi } from "../../clients/conversations/sessionMetadataApi";
import type { ProfileApi, ProfileDefinition } from "../../clients/profiles/profileApi";
import type { VaultApi } from "../../state/vaultApi";
import { fakeDurable } from "../../testDurable";
import {
  type CapabilitiesOverrides,
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

const PROFILE = "00000000-0000-4000-8000-000000000001";
const HIT_SESSION = "00000000-0000-4000-8000-000000000009";
const AT = "2026-09-21T00:00:00.000Z";

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function vaultApi(): VaultApi {
  const pending = () => new Promise<never>(() => {});
  return {
    entries: pending,
    note: pending,
    backlinks: pending,
    history: pending,
    bookmarks: async () => ({ bookmarks: [] }),
    addBookmark: pending,
    removeBookmark: pending,
    createNote: pending,
    saveNote: pending,
    recovery: async () => [],
  };
}

function profileApi(): ProfileApi {
  const definition: ProfileDefinition = {
    profile_id: PROFILE,
    revision: 1,
    current_revision: 1,
    state_revision: 1,
    created_at: AT,
    updated_at: AT,
    archived: false,
    document: {
      format_version: 1,
      identity: {
        name: "Alpha",
        purpose: "",
        avatar_asset_id: null,
        accent: "blue",
        shape: "rounded",
      },
      runtime: { provider: "claude", model: null, effort: "high", permission_mode: null },
      instructions: "",
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
    },
  };
  const { document, ...row } = definition;
  const pending = () => new Promise<never>(() => {});
  return {
    list: async () => [
      {
        ...row,
        identity: document.identity,
        runtime: document.runtime,
        provenance: document.provenance,
      },
    ],
    get: async () => definition,
    create: pending,
    revise: pending,
    archive: pending,
    duplicate: pending,
    compare: pending,
  };
}

interface MountOptions {
  capabilities?: CapabilitiesOverrides;
  organization?: SessionMetadataApi;
  vault?: boolean;
  chatOnly?: boolean;
  workspace?: Partial<WorkspaceJson>;
}

function mount({
  capabilities = {},
  organization,
  vault = true,
  chatOnly = false,
  workspace = {},
}: MountOptions = {}) {
  const { viki, socket } = fakeViki();
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture(capabilities),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspaceFixture({ root_physical_id: "12:34", ...workspace })]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: chatOnly,
    },
  });
  const rest = fakeRest({
    hits: [{ session_id: HIT_SESSION, title: "Palette hit", role: "title", snippet: "", at: "" }],
  });
  const utils = render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      shell={shell}
      profileApi={profileApi()}
      {...(vault ? { vaultApi: vaultApi() } : {})}
      {...(organization ? { organization } : {})}
    />,
  );
  return { ...utils, viki, socket };
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}

async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.click(element);
  });
}

async function pressEscape(target: Window | HTMLElement = window): Promise<void> {
  await act(async () => {
    fireEvent.keyDown(target, { key: "Escape" });
  });
}

function inVault(): boolean {
  return screen.queryByRole("region", { name: "Vault browser" }) !== null;
}

function inAgents(): boolean {
  return screen.queryByRole("region", { name: "Profiles" }) !== null;
}

function inChat(container: HTMLElement): boolean {
  return container.querySelector('[data-region="conversation"]') !== null;
}

/** The Vault view with an unsaved new note in its editor. */
async function dirtyVault(): Promise<void> {
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  await click(screen.getByRole("button", { name: "New note" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Note path" }), {
    target: { value: "new.md" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Markdown" }), {
    target: { value: "draft text" },
  });
}

function markdown(): string {
  return (screen.getByRole("textbox", { name: "Markdown" }) as HTMLTextAreaElement).value;
}

/** The Agents view with an unsaved rename of its one profile. */
async function dirtyAgents(): Promise<void> {
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  await settled();
  await click(screen.getByRole("button", { name: /Alpha — claude/ }));
  await settled();
  fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
    target: { value: "Changed" },
  });
}

function profileName(): string {
  return (screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value;
}

test("Escape in a dirty Vault view asks; cancel keeps the view and the draft, confirm leaves", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { container } = mount();
  await settled();
  await dirtyVault();

  await pressEscape();
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(confirm).toHaveBeenCalledWith("Your unsaved changes will be lost. Leave this view?");
  expect(inVault()).toBe(true);
  expect(markdown()).toBe("draft text");

  confirm.mockReturnValue(true);
  await pressEscape();
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(inChat(container)).toBe(true);
});

test("Escape typed inside a field of a dirty view does nothing", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  mount();
  await settled();
  await dirtyVault();
  const box = screen.getByRole("textbox", { name: "Markdown" });
  box.focus();

  // The second Escape after cancelling an IME conversion arrives exactly like this.
  await pressEscape(box);
  expect(confirm).not.toHaveBeenCalled();
  expect(inVault()).toBe(true);
  expect(markdown()).toBe("draft text");
});

test("the Chat tab and the back button ask before leaving a dirty Vault view", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { container } = mount();
  await settled();
  await dirtyVault();

  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  await click(screen.getByRole("button", { name: "Back" }));
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(inVault()).toBe(true);
  expect(markdown()).toBe("draft text");

  confirm.mockReturnValue(true);
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  expect(inChat(container)).toBe(true);
});

test("every other way out of a dirty Agents view asks, and a cancel keeps the edit", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { container, viki } = mount();
  await settled();
  await dirtyAgents();

  // Ctrl+,
  await act(async () => {
    fireEvent.keyDown(window, { key: ",", ctrlKey: true });
  });
  // an app-menu command
  await click(screen.getByRole("button", { name: "App menu" }));
  await click(screen.getByRole("menuitem", { name: /Data locations/ }));
  // the sidebar Chat destination
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  // the sidebar Vault destination
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  // a palette hit
  await act(async () => {
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  });
  fireEvent.change(screen.getByPlaceholderText("Search sessions"), { target: { value: "hit" } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(150);
  });
  await click(screen.getByText("Palette hit"));

  expect(confirm).toHaveBeenCalledTimes(5);
  expect(inAgents()).toBe(true);
  expect(profileName()).toBe("Changed");
  // The cancelled hit opened nothing behind the view.
  expect(viki.store.getSnapshot().sessions[HIT_SESSION]).toBeUndefined();

  confirm.mockReturnValue(true);
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Chat" },
    ),
  );
  expect(inChat(container)).toBe(true);
});

test("beforeunload is blocked while a view holds unsaved work, and only then", async () => {
  mount();
  await settled();
  await dirtyVault();
  const dirty = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(dirty);
  expect(dirty.defaultPrevented).toBe(true);

  await click(screen.getByRole("button", { name: "Cancel" }));
  const clean = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(clean);
  expect(clean.defaultPrevented).toBe(false);
});

test("clean views leave without asking, and each focuses its heading on entry", async () => {
  const confirm = vi.spyOn(window, "confirm");
  const { container } = mount();
  await settled();

  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 }));
  await pressEscape();
  expect(inChat(container)).toBe(true);

  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  await settled();
  expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Agents" }));
  expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
  await click(screen.getByRole("button", { name: /Alpha — claude/ }));
  await settled();
  await click(screen.getByRole("button", { name: "Back" }));
  expect(inChat(container)).toBe(true);
  expect(confirm).not.toHaveBeenCalled();
});

test("a reload lands back in the Vault or Agents view, but only where the view exists", async () => {
  window.sessionStorage.setItem("viki.view", JSON.stringify({ view: "vault" }));
  mount();
  await settled();
  expect(inVault()).toBe(true);
  cleanup();

  window.sessionStorage.setItem("viki.view", JSON.stringify({ view: "agents" }));
  mount();
  await settled();
  expect(inAgents()).toBe(true);
  cleanup();

  // A window without the vault client has no Vault view to land in.
  window.sessionStorage.setItem("viki.view", JSON.stringify({ view: "vault" }));
  const { container } = mount({ vault: false });
  await settled();
  expect(inChat(container)).toBe(true);
});

test("the no-client Vault section focuses its heading and its back button leaves; Escape after a cancelled prompt elsewhere leaves focus where it was", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { container } = mount({ chatOnly: true });
  await settled();

  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  const heading = screen.getByRole("heading", { level: 1, name: "Vault" });
  expect(document.activeElement).toBe(heading);

  await click(screen.getByRole("button", { name: "Back" }));
  expect(inChat(container)).toBe(true);
  expect(confirm).not.toHaveBeenCalled(); // this section holds no edit, so leaving never asks

  await dirtyAgents();
  const away = screen.getByRole("heading", { level: 1, name: "Profiles" });
  away.focus();
  expect(document.activeElement).toBe(away);

  await pressEscape();
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(inAgents()).toBe(true);
  expect(document.activeElement).toBe(away);
});

test("a recovery window sends no session-metadata request; a normal one sends one", async () => {
  const recovery = { list: vi.fn(async () => []), patch: vi.fn(), remove: vi.fn() };
  mount({
    capabilities: { app: { mode: "recovery" }, storage: { state: "unavailable", reason: "busy" } },
    organization: recovery,
  });
  await settled();
  expect(recovery.list).not.toHaveBeenCalled();
  cleanup();

  const normal = { list: vi.fn(async () => []), patch: vi.fn(), remove: vi.fn() };
  mount({ organization: normal });
  await settled();
  expect(normal.list).toHaveBeenCalledTimes(1);
});

test("a protected workspace's Vault view offers no New note", async () => {
  mount({ workspace: { policy: "protected" } });
  await settled();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  expect(screen.queryByRole("button", { name: "New note" })).toBeNull();
});

test("a workspace with a pending bootstrap offers no New note", async () => {
  mount({ workspace: { bootstrap: "pending" } });
  await settled();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  expect(screen.queryByRole("button", { name: "New note" })).toBeNull();
});
