import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import { ShellApiError } from "../../clients/app/shellApi";
import { t } from "../../i18n/t";
import { createShellStore, type ShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import type { FakeShellApi } from "../../testShell";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import { OnboardingView } from "./OnboardingView";

async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

async function readyStore(
  overrides: Parameters<typeof fakeShellApi>[0] = {},
): Promise<{ api: FakeShellApi; store: ShellStore }> {
  const api = fakeShellApi(overrides);
  const store = createShellStore(api, new AppStore());
  store.start();
  await flush();
  return { api, store };
}

function renderView(
  store: ShellStore,
  api: FakeShellApi,
  extra: Partial<Parameters<typeof OnboardingView>[0]> = {},
  modelConnections: Parameters<typeof ModelConnectionsContext.Provider>[0]["value"] = null,
) {
  const onMode = vi.fn();
  const onDone = vi.fn();
  const onNavigate = vi.fn();
  const renderInspection = vi.fn(() => <div data-testid="inspection" />);
  const utils = render(
    <ModelConnectionsContext.Provider value={modelConnections}>
      <OnboardingView
        store={store}
        api={api}
        mode="choose"
        onMode={onMode}
        onDone={onDone}
        onNavigate={onNavigate}
        renderInspection={renderInspection}
        {...extra}
      />
    </ModelConnectionsContext.Provider>,
  );
  return { ...utils, onMode, onDone, onNavigate, renderInspection };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** A model connections api whose list holds the given model refs, or fails without any. */
function connectionsApi(refs: readonly string[] | null) {
  return {
    list: vi.fn(async () => {
      if (refs === null) throw new Error("unavailable");
      return {
        revision: 1,
        default_model: refs[0] ?? null,
        connections: refs.map((model_ref) => ({ model_ref, name: "Local", model: "model" })),
      };
    }),
  } as never;
}

function stepNamed(name: string): HTMLElement {
  return within(screen.getByRole("list", { name: "Setup steps" })).getByRole("listitem", { name });
}

describe("the first run is a three-step setup guide", () => {
  it("lists the steps in order, each with a visible state", async () => {
    const { store, api } = await readyStore();
    renderView(store, api);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Welcome to VIKI");
    const items = within(screen.getByRole("list", { name: "Setup steps" })).getAllByRole(
      "listitem",
    );
    expect(items.map((item) => item.querySelector("h2")?.textContent)).toEqual([
      "1. Install Claude Code",
      "2. Sign in to Claude, or connect a model",
      "3. Create or open a vault",
    ]);
    expect(items.map((item) => item.getAttribute("data-state"))).toEqual([
      "done",
      "done",
      "needed",
    ]);
    expect(within(stepNamed("Install Claude Code")).getByText("Done")).toBeTruthy();
    expect(
      within(stepNamed("Install Claude Code")).getByText("Found Claude Code 1.0.0."),
    ).toBeTruthy();
    expect(
      within(stepNamed("Sign in to Claude, or connect a model")).getByText("Done"),
    ).toBeTruthy();
    expect(within(stepNamed("Create or open a vault")).getByText("Needed")).toBeTruthy();
  });

  it("keeps the three cards inside step 3 with their ids", async () => {
    const { store, api } = await readyStore();
    renderView(store, api);
    const group = within(stepNamed("Create or open a vault")).getByRole("group", {
      name: "Create or open a vault",
    });
    expect(
      within(group)
        .getAllByRole("button")
        .map((button) => button.id),
    ).toEqual(["onboarding-card-create", "onboarding-card-open", "onboarding-card-later"]);
  });

  it("lists the six optional features and the footer", async () => {
    const { store, api } = await readyStore();
    renderView(store, api);
    const optional = screen.getByRole("region", { name: "Optional, any time" });
    const items = within(optional).getAllByRole("listitem");
    expect(items).toHaveLength(6);
    expect(items[0]?.textContent).toMatch(/^Bots and Kanban:/);
    expect(items[5]?.textContent).toMatch(/^Messaging:/);
    screen.getByText(/Open this guide again from the Help menu/);
    screen.getByText(/^Steps 1 and 2 let you start chatting/);
  });

  it("with Claude Code missing: step 1 needed, guide link and provider setup, step 2 waits", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { cli: "missing", can_start: false } }),
    });
    const onOpenUrl = vi.fn();
    const signIn = { open: vi.fn() };
    const { onNavigate } = renderView(store, api, { onOpenUrl, signIn });
    const claude = stepNamed("Install Claude Code");
    expect(claude.getAttribute("data-state")).toBe("needed");
    within(claude).getByText("Needed");
    within(claude).getByText(/restart VIKI Agent/i);
    fireEvent.click(within(claude).getByRole("button", { name: "Anthropic's setup guide" }));
    expect(onOpenUrl).toHaveBeenCalledExactlyOnceWith("https://code.claude.com/docs/en/setup");
    fireEvent.click(within(claude).getByRole("button", { name: "Set up provider" }));
    expect(onNavigate).toHaveBeenCalledWith({ view: "settings", page: "providers" });
    const model = stepNamed("Sign in to Claude, or connect a model");
    within(model).getByText("After step 1");
    expect(within(model).queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("omits the guide link without onOpenUrl", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { cli: "missing", can_start: false } }),
    });
    renderView(store, api);
    expect(screen.queryByRole("button", { name: "Anthropic's setup guide" })).toBeNull();
  });

  it("an older Claude Code is done with an update note", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { version_status: "older" } }),
    });
    renderView(store, api);
    const claude = stepNamed("Install Claude Code");
    within(claude).getByText(
      /older than the ones VIKI Agent was tested with \(2\.1\.281 to 2\.1\.282\)/,
    );
    within(claude).getByRole("button", { name: "Set up provider" });
  });

  it("login unknown: not checked yet, Sign in calls the login api once, no model setup without connections", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { login: "unknown" } }),
    });
    const signIn = { open: vi.fn().mockResolvedValue(undefined) };
    const { onNavigate } = renderView(store, api, { signIn });
    const model = stepNamed("Sign in to Claude, or connect a model");
    expect(model.getAttribute("data-state")).toBe("unchecked");
    within(model).getByText("Not checked yet");
    fireEvent.click(within(model).getByRole("button", { name: "Sign in" }));
    await act(async () => {});
    expect(signIn.open).toHaveBeenCalledTimes(1);
    within(model).getByText(/^Finish signing in in your browser/);
    expect(within(model).queryByRole("button", { name: "Choose a model provider" })).toBeNull();
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("login unknown with model connections: not checked yet, and model setup is offered", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { login: "unknown" } }),
    });
    const { onNavigate } = renderView(store, api, {}, connectionsApi([]));
    const model = stepNamed("Sign in to Claude, or connect a model");
    expect(model.getAttribute("data-state")).toBe("unchecked");
    within(model).getByText(/^VIKI Agent checks your sign-in when you start a chat/);
    fireEvent.click(within(model).getByRole("button", { name: "Choose a model provider" }));
    expect(onNavigate).toHaveBeenCalledWith({ view: "settings", page: "providers" });
  });

  it("offers Choose a model provider when model connections exist", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { login: "signed_out" } }),
    });
    const { onNavigate } = renderView(store, api, {}, { list: vi.fn() } as never);
    const model = stepNamed("Sign in to Claude, or connect a model");
    within(model).getByText("Needed");
    within(model).getByText(/^Claude Code isn't signed in/);
    fireEvent.click(within(model).getByRole("button", { name: "Choose a model provider" }));
    expect(onNavigate).toHaveBeenCalledWith({ view: "settings", page: "providers" });
  });

  it("without a signIn prop there is no Sign in button", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { login: "unknown" } }),
    });
    renderView(store, api);
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("a blocked billing state is needed with the billing text and provider setup", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { billing: "blocked", problems: ["x"] } }),
    });
    renderView(store, api, { signIn: { open: vi.fn() } });
    const model = stepNamed("Sign in to Claude, or connect a model");
    expect(model.getAttribute("data-state")).toBe("needed");
    within(model).getByText("Needed");
    within(model).getByText(t("workspaces.setup.billing"));
    expect(within(model).queryByText(/Model connections need Claude Code/)).toBeNull();
    within(model).getByRole("button", { name: "Set up provider" });
    expect(within(model).queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("a connection default is done with the connection text", async () => {
    const { store, api } = await readyStore({
      settings: settingsFixture({ "chat.default_model": "viki-llm/abc/model" }),
      capabilities: capabilitiesFixture({ provider: { login: "unknown" } }),
    });
    renderView(store, api, { signIn: { open: vi.fn() } }, connectionsApi(["viki-llm/abc/model"]));
    await act(async () => {});
    const model = stepNamed("Sign in to Claude, or connect a model");
    expect(model.getAttribute("data-state")).toBe("done");
    within(model).getByText("New chats use your model connection.");
    expect(within(model).queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("a connection default on an unknown Claude Code version is needed, with the minimum", async () => {
    const { store, api } = await readyStore({
      settings: settingsFixture({ "chat.default_model": "viki-llm/abc/model" }),
      capabilities: capabilitiesFixture({ provider: { version: "", version_status: "unknown" } }),
    });
    const { onNavigate } = renderView(store, api, {}, connectionsApi(["viki-llm/abc/model"]));
    await act(async () => {});
    const model = stepNamed("Sign in to Claude, or connect a model");
    expect(model.getAttribute("data-state")).toBe("needed");
    within(model).getByText(/Model connections need Claude Code 2\.1\.281 or later/);
    fireEvent.click(within(model).getByRole("button", { name: "Set up provider" }));
    expect(onNavigate).toHaveBeenCalledWith({ view: "settings", page: "providers" });
  });

  it.each([
    ["cannot be listed", null, "unchecked", /list of connections couldn't be read/],
    ["is no longer listed", ["viki-llm/other/model"], "needed", /no longer exists/],
  ] as const)(
    "a connection default whose connection %s is not done, and points to Providers",
    async (_name, refs, state, text) => {
      const { store, api } = await readyStore({
        settings: settingsFixture({ "chat.default_model": "viki-llm/abc/model" }),
      });
      const { onNavigate } = renderView(store, api, {}, connectionsApi(refs));
      await act(async () => {});
      const model = stepNamed("Sign in to Claude, or connect a model");
      expect(model.getAttribute("data-state")).toBe(state);
      within(model).getByText(text);
      expect(within(model).queryByText("New chats use your model connection.")).toBeNull();
      fireEvent.click(within(model).getByRole("button", { name: "Set up provider" }));
      expect(onNavigate).toHaveBeenCalledWith({ view: "settings", page: "providers" });
    },
  );

  it("shows each step's number in its heading, matching the lead and the pill", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ provider: { cli: "missing", can_start: false } }),
    });
    renderView(store, api);
    const items = within(screen.getByRole("list", { name: "Setup steps" })).getAllByRole(
      "listitem",
    );
    expect(items.map((item) => item.querySelector("h2")?.textContent)).toEqual([
      "1. Install Claude Code",
      "2. Sign in to Claude, or connect a model",
      "3. Create or open a vault",
    ]);
    screen.getByText(/^Steps 1 and 2 let you start chatting. Step 3 is needed/);
    within(items[1] as HTMLElement).getByText("After step 1");
  });

  it("a registered vault awaiting restart replaces the cards with the restart text", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({
        workspace: { restart_required: true, selected_id: "22222222-2222-4222-8222-222222222222" },
      }),
      workspaces: listingFixture([workspaceFixture()]),
    });
    renderView(store, api);
    const vault = stepNamed("Create or open a vault");
    within(vault).getByText("Restart needed");
    within(vault).getByText(/Restart VIKI Agent to start using it/);
    expect(within(vault).queryByRole("group")).toBeNull();
  });

  it("an active vault is done with its name as text", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({
        workspace: {
          chat_only: false,
          active: {
            id: "v",
            kind: "vault",
            name: "<b>My notes</b>",
            policy: "ordinary",
            layout: "unknown",
            source: "registration",
            aliased: false,
            root_available: true,
          },
        },
      }),
    });
    renderView(store, api);
    const vault = stepNamed("Create or open a vault");
    within(vault).getByText("Using <b>My notes</b>.");
    expect(vault.querySelector("b")).toBeNull();
    expect(within(vault).queryByRole("group")).toBeNull();
  });

  it("step states change in place when the store commits", async () => {
    const { store, api } = await readyStore();
    renderView(store, api);
    expect(stepNamed("Install Claude Code").getAttribute("data-state")).toBe("done");
    api.data.capabilities = capabilitiesFixture({ provider: { cli: "missing", can_start: false } });
    await act(async () => {
      await store.afterMutation(["capabilities"]);
    });
    expect(stepNamed("Install Claude Code").getAttribute("data-state")).toBe("needed");
    expect(stepNamed("Sign in to Claude, or connect a model").getAttribute("data-state")).toBe(
      "waiting",
    );
  });
});

describe("the first run offers exactly three choices", () => {
  it("renders exactly the create, open and later cards, and no fourth (Git) card", async () => {
    const { store, api } = await readyStore();
    renderView(store, api);
    const group = screen.getByRole("group", { name: "Create or open a vault" });
    const buttons = within(group).getAllByRole("button");
    expect(buttons).toHaveLength(3);
    screen.getByText("Create a vault"); // throws if absent
    screen.getByText("Open a folder"); // throws if absent
    screen.getByText("Set up later"); // throws if absent
    expect(screen.queryByText(/git/i)).toBeNull();
  });

  it("focuses the h1 on mount", async () => {
    const { store, api } = await readyStore();
    renderView(store, api);
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 }));
  });

  it("clicking Create swaps the cards for the create form", async () => {
    const { store, api } = await readyStore();
    const { onMode } = renderView(store, api);
    fireEvent.click(screen.getByText("Create a vault"));
    expect(onMode).toHaveBeenCalledWith("create");
  });

  it("Back returns from a form to the cards", async () => {
    const { store, api } = await readyStore();
    const { onMode } = renderView(store, api, { mode: "create" });
    fireEvent.click(screen.getByText("Back"));
    expect(onMode).toHaveBeenCalledWith("choose");
  });

  it("Escape on a form returns to the cards", async () => {
    const { store, api } = await readyStore();
    const { onMode } = renderView(store, api, { mode: "open" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onMode).toHaveBeenCalledWith("choose");
  });

  it("Escape is ignored while composing (IME guard)", async () => {
    const { store, api } = await readyStore();
    const { onMode } = renderView(store, api, { mode: "open" });
    fireEvent.keyDown(window, { key: "Escape", isComposing: true });
    expect(onMode).not.toHaveBeenCalled();
  });

  it("with the app menu open over a form, Escape closes only the menu — the view stands down", async () => {
    const { store, api } = await readyStore();
    // A stand-in for AppMenu.tsx's own open panel: this component never imports AppMenu,
    // so the check it performs is on the DOM shape alone, the same shape
    // AppMenu.tsx's `role="menu"` panel has when open.
    render(<div role="menu" aria-label="App menu" />);
    const { onMode } = renderView(store, api, { mode: "create" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onMode).not.toHaveBeenCalled();
  });

  it("a refused card is focusable, aria-disabled, and its click is a no-op", async () => {
    const { store, api } = await readyStore({
      capabilities: capabilitiesFixture({ storage: { state: "unavailable" } }),
    });
    const { onMode } = renderView(store, api);
    const createCard = screen.getByText("Create a vault").closest("button");
    expect(createCard).not.toBeNull();
    expect(createCard?.getAttribute("aria-disabled")).toBe("true");
    expect(createCard?.tabIndex).not.toBe(-1);
    fireEvent.click(createCard as HTMLButtonElement);
    expect(onMode).not.toHaveBeenCalled();
  });
});

describe("later is the user's choice even when the save fails", () => {
  it("patches onboarding.completed and returns to chat on success", async () => {
    const { store, api } = await readyStore();
    const { onDone } = renderView(store, api);
    fireEvent.click(screen.getByText("Set up later"));
    await flush();
    expect(api.calls.some((call) => call.method === "patchSettings")).toBe(true);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("still returns to chat when the patch fails", async () => {
    const { store, api } = await readyStore();
    api.fail("patchSettings", new ShellApiError(503, "unavailable"));
    const { onDone } = renderView(store, api);
    fireEvent.click(screen.getByText("Set up later"));
    await flush();
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});

describe("a create hands its result up through onVaultCreated", () => {
  it("calls onVaultCreated once after a successful create", async () => {
    const { store, api } = await readyStore();
    const onVaultCreated = vi.fn();
    renderView(store, api, { mode: "create", onVaultCreated });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
    fireEvent.blur(screen.getByLabelText("Name"));
    await flush();
    fireEvent.click(screen.getByText("Create"));
    await flush();
    expect(onVaultCreated).toHaveBeenCalledTimes(1);
  });
});
