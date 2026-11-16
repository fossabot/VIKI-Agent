import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { RoutinesApi } from "../../clients/automation/routinesApi";
import type { CodeApi } from "../../clients/code/codeApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
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

const pending = () => new Promise<never>(() => {});
const profiles: ProfileApi = {
  list: async () => [],
  get: pending,
  create: pending,
  revise: pending,
  archive: pending,
  duplicate: pending,
  compare: pending,
};
const vault: VaultApi = {
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
const code: CodeApi = {
  tree: pending,
  file: pending,
  search: pending,
  changes: pending,
  diff: pending,
  save: pending,
};
const routines: RoutinesApi = {
  list: async () => ({
    routines: [],
    scheduler: { state: "running", attended: true, paused: false },
  }),
  get: pending,
  runs: pending,
  preview: pending,
  create: pending,
  revise: pending,
  setState: pending,
  run: pending,
};

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount(options: { narrow?: boolean; capabilities?: CapabilitiesOverrides } = {}) {
  if (options.narrow) {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
  }
  const fake = fakeViki();
  const workspace = workspaceFixture({
    kind: "code",
    root_physical_id: "12:34",
  });
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: { chat_only: false },
      ...options.capabilities,
    }),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspace]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(
      <App
        viki={fake.viki}
        rest={fakeRest()}
        durable={fakeDurable()}
        shell={shell}
        profileApi={profiles}
        vaultApi={vault}
        codeApi={code}
        routinesApi={routines}
      />,
    );
    fake.socket.open();
  });
  return { ...result, ...fake };
}

const sidebar = () => screen.getByRole("navigation", { name: "Navigation and sessions" });
const action = (name: string) => within(sidebar()).getByRole("button", { name });
async function click(element: HTMLElement) {
  await act(async () => {
    element.focus();
    fireEvent.click(element);
  });
}
async function openDrawer() {
  await click(screen.getByRole("button", { name: "Sessions and agents" }));
  const drawer = screen.getByRole("dialog", { name: "Sessions and agents" });
  expect(within(drawer).queryByRole("button", { name: "Code" })).toBeNull();
  return drawer;
}
async function openNarrowVault() {
  await openDrawer();
  await click(action("Vault"));
  expect(screen.getByRole("region", { name: "Vault browser" })).toBeTruthy();
}
async function dirtyVault() {
  await click(screen.getByRole("button", { name: "New note" }));
  await act(async () => {
    fireEvent.change(screen.getByRole("textbox", { name: "Note path" }), {
      target: { value: "draft.md" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Markdown" }), {
      target: { value: "Synthetic unsaved draft" },
    });
  });
  return screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Markdown" });
}

test("sidebar navigation remains reachable across every available view without duplicate top-bar controls", async () => {
  const { container } = await mount();
  const top = container.querySelector('[data-region="topbar"]') as HTMLElement;
  const assertSidebar = () => {
    expect(sidebar()).toBeTruthy();
    expect(within(sidebar()).queryByText("VIKI Agent")).toBeNull();
    expect(within(top).queryByRole("tablist", { name: "Primary" })).toBeNull();
    expect(within(top).queryByRole("combobox", { name: "Primary view" })).toBeNull();
    expect(within(top).queryByRole("button", { name: "New session" })).toBeNull();
    expect(within(top).queryByRole("button", { name: "New task" })).toBeNull();
  };
  assertSidebar();
  expect(action("Chat").getAttribute("aria-current")).toBe("page");
  expect(within(sidebar()).queryByRole("button", { name: "Code" })).toBeNull();
  for (const name of ["Capabilities", "Vault", "Agents", "Routines", "Chat"]) {
    await click(action(name));
    assertSidebar();
    expect(action(name).getAttribute("aria-current")).toBe("page");
  }
  await click(action("Choose workspace"));
  assertSidebar();
  expect(container.querySelector('[data-region="view"]')).not.toBeNull();
  expect(action("New session")).toBeTruthy();
  expect(within(sidebar()).queryByRole("button", { name: "New task" })).toBeNull();
});

test.each(["Chat", "New session"])(
  "a clean Vault admits %s from the modal sidebar and closes the drawer",
  async (name) => {
    const { container, socket } = await mount({ narrow: true });
    await openNarrowVault();
    await openDrawer();
    socket.sent.length = 0;
    await click(action(name));
    expect(screen.queryByRole("dialog", { name: "Sessions and agents" })).toBeNull();
    expect(container.querySelector('[data-region="conversation"]')).not.toBeNull();
    expect(socket.sent.filter((item) => item.type === "session.open")).toHaveLength(
      name === "New session" ? 1 : 0,
    );
  },
);

test.each(["Chat", "New session"])(
  "cancelling %s from a dirty Vault keeps the draft and sidebar drawer",
  async (name) => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { socket } = await mount({ narrow: true });
    await openNarrowVault();
    const editor = await dirtyVault();
    const drawer = await openDrawer();
    socket.sent.length = 0;
    await click(action(name));
    expect(confirm).toHaveBeenCalledExactlyOnceWith(
      "Your unsaved changes will be lost. Leave this view?",
    );
    expect(screen.getByRole("dialog", { name: "Sessions and agents" })).toBe(drawer);
    expect(editor.isConnected).toBe(true);
    expect(editor.value).toBe("Synthetic unsaved draft");
    expect(socket.sent.filter((item) => item.type === "session.open")).toHaveLength(0);
  },
);

test("Alt+N keeps the dirty Vault and its sidebar drawer unchanged while the drawer is modal", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { container, socket } = await mount({ narrow: true });
  await openNarrowVault();
  const editor = await dirtyVault();
  const drawer = await openDrawer();
  const focused = document.activeElement;
  socket.sent.length = 0;
  await act(async () => {
    fireEvent.keyDown(window, { key: "n", altKey: true });
  });
  expect(screen.getByRole("dialog", { name: "Sessions and agents" })).toBe(drawer);
  expect(container.querySelector('[data-region="conversation"]')).toBeNull();
  expect(editor.isConnected).toBe(true);
  expect(editor.value).toBe("Synthetic unsaved draft");
  expect(document.activeElement).toBe(focused);
  expect(confirm).not.toHaveBeenCalled();
  expect(socket.sent.filter((item) => item.type === "session.open")).toHaveLength(0);
});

test("provider readiness keeps the sidebar creation actions focusable but refused with their reason", async () => {
  const { socket } = await mount({
    capabilities: { provider: { can_start: false, login: "signed_out" } },
  });
  socket.sent.length = 0;
  for (const name of ["New session"]) {
    const button = action(name) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.getAttribute("aria-describedby")).toBe("readiness-reason");
    expect(document.getElementById("readiness-reason")?.textContent).toBeTruthy();
    await click(button);
    expect(document.activeElement).toBe(button);
  }
  expect(socket.sent.filter((item) => item.type === "session.open")).toHaveLength(0);
});
