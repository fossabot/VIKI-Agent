/**
 * The Code view as App wires it. The Workbench is stubbed to its props (its own suite covers its
 * screens): the folders and active id App hands it, a quote and an open it sends back, and the
 * Code tab, the restored view and the vault surfaces a code folder turns off.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { emptyRails, fakeRest, fakeViki, gate } from "../../accept/m3b/fakes";
import type { CodeApi } from "../../clients/code/codeApi";
import type { WorkbenchProps } from "../../components/Workbench";
import type { Rails } from "../../protocol";
import { dropBuffer, getBuffer, putBuffer } from "../../state/codeBuffers";
import { setUnsavedWork } from "../../state/unsavedWork";
import type { VaultApi } from "../../state/vaultApi";
import type { VaultOpsApi } from "../../state/vaultOpsApi";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import { openWorkbench } from "../../testWorkbench";

vi.mock("../../components/Workbench", () => ({
  Workbench: (props: WorkbenchProps) => (
    <section>
      <h1>Workbench stub</h1>
      <p data-testid="folders">{props.folders.map((folder) => folder.name).join(",")}</p>
      <p data-testid="active">{props.activeId ?? "none"}</p>
      <p data-testid="editor">
        {`${props.fontSize}|${props.lineWrap}|${props.recovery ? "recovery" : "none"}`}
      </p>
      <section className="code-editor">
        {/* biome-ignore lint/a11y/useSemanticElements: the stub stands for a contenteditable editor */}
        <div
          contentEditable
          suppressContentEditableWarning
          role="textbox"
          aria-label="stub editor"
          tabIndex={0}
          data-testid="stub-editor"
        />
        <input aria-label="stub find" />
      </section>
      <button type="button" onClick={() => props.recovery?.list()}>
        recovery list
      </button>
      <button type="button" onClick={() => props.recovery?.forget("op-1")}>
        recovery forget
      </button>
      <button type="button" onClick={() => props.onQuote("`src/a.ts:3-5`")}>
        quote
      </button>
      <button type="button" onClick={() => props.onOpenInEditor(FAKE_WORKSPACE_ID, "src/a.ts", 3)}>
        open
      </button>
      <button type="button" onClick={props.onOpenSettings}>
        settings
      </button>
      {props.renderBack?.(props.onBack) ?? (
        <button type="button" onClick={props.onBack}>
          back
        </button>
      )}
    </section>
  ),
}));

const CODE_ID = "55555555-5555-4555-8555-555555555555";

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  delete (window.navigator as { platform?: string }).platform;
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

function codeApi(): CodeApi {
  const pending = () => new Promise<never>(() => {});
  return {
    tree: pending,
    file: pending,
    search: pending,
    changes: pending,
    diff: pending,
    save: pending,
  };
}

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

/** A vault and a code folder in the listing; `active` says which one the window is on. */
function shellApi(active: "vault" | "code" | null, effective: Record<string, unknown> = {}) {
  const stored = settingsFixture({ "onboarding.completed": true });
  const settings = { ...stored, effective: { ...stored.effective, ...effective } };
  const code = workspaceFixture({ workspace_id: CODE_ID, kind: "code", name: "app code" });
  const vault = workspaceFixture({ root_physical_id: "12:34" });
  const base = capabilitiesFixture();
  const row = active === "code" ? code : vault;
  return fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: active === null,
        active:
          active === null
            ? base.workspace.active
            : {
                id: row.workspace_id,
                kind: row.kind,
                name: row.name,
                policy: row.policy,
                layout: "unknown",
                source: "created",
                aliased: false,
                root_available: true,
              },
      },
    }),
    settings,
    workspaces: {
      ...listingFixture([vault, code]),
      active_id: row.workspace_id,
      chat_only: active === null,
    },
  });
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

const draftOf = () => (screen.getByLabelText("Message") as HTMLTextAreaElement).value;

function mount(
  options: {
    code?: boolean;
    active?: "vault" | "code" | null;
    rails?: Rails;
    effective?: Record<string, unknown>;
    ops?: VaultOpsApi;
  } = {},
) {
  const fake = fakeViki();
  const shell = shellApi(options.active ?? "code", options.effective);
  const { container } = render(
    <App
      viki={fake.viki}
      rest={fakeRest(options.rails ? { rails: options.rails } : {})}
      durable={fakeDurable()}
      shell={shell}
      vaultApi={vaultApi()}
      {...(options.ops ? { vaultOpsApi: options.ops } : {})}
      {...(options.code === false ? {} : { codeApi: codeApi() })}
    />,
  );
  act(() => fake.socket.open());
  return { ...fake, container, shell };
}

test("the explicit right Code entry shows the Workbench with the code folders and the active one", async () => {
  mount();
  await settled();
  expect(screen.queryByText("Workbench stub")).toBeNull();
  await openWorkbench();
  expect(screen.getByText("Workbench stub")).toBeTruthy();
  expect(JSON.parse(window.sessionStorage.getItem("viki.view") ?? "null")).toMatchObject({
    view: "code",
  });
  expect(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).queryByRole(
      "button",
      { name: "Code" },
    ),
  ).toBeNull();
  expect(screen.getByTestId("folders").textContent).toBe("app code");
  expect(screen.getByTestId("active").textContent).toBe(CODE_ID);
});

test("Escape inside the code editor stays in the Code view; elsewhere in it, Escape returns to the chat", async () => {
  mount();
  await settled();
  await openWorkbench();
  const escapeFrom = (element: HTMLElement) => {
    act(() => element.focus());
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
  };
  escapeFrom(screen.getByTestId("stub-editor"));
  expect(screen.getByText("Workbench stub")).toBeTruthy();
  escapeFrom(screen.getByLabelText("stub find"));
  expect(screen.getByText("Workbench stub")).toBeTruthy();
  escapeFrom(screen.getByRole("button", { name: "recovery list" }));
  expect(screen.queryByText("Workbench stub")).toBeNull();
});

function opsApi(): VaultOpsApi {
  return {
    preview: vi.fn(),
    apply: vi.fn(),
    recovery: vi.fn(),
    forgetOperation: vi.fn(),
    resolve: vi.fn(async () => ({}) as never),
  };
}

function setPlatform(platform: string) {
  Object.defineProperty(window.navigator, "platform", { value: platform, configurable: true });
}

test("passes the editor settings to the Workbench", async () => {
  setPlatform("Win32");
  const ops = opsApi();
  mount({
    effective: { "workbench.code_font_size": 16, "workbench.line_wrap": true },
    ops,
  });
  await settled();
  await openWorkbench();
  expect(screen.getByTestId("editor").textContent).toBe("16|true|recovery");
  await click(screen.getByRole("button", { name: "recovery list" }));
  await click(screen.getByRole("button", { name: "recovery forget" }));
  expect(ops.resolve).toHaveBeenCalledWith("op-1", "forget");
});

test("the editor settings fall back off Windows, out of range and without an ops client", async () => {
  setPlatform("MacIntel");
  mount({ effective: { "workbench.code_font_size": 30, "workbench.line_wrap": "yes" } });
  await settled();
  await openWorkbench();
  expect(screen.getByTestId("editor").textContent).toBe("13|false|none");
  cleanup();
  mount({ effective: { "workbench.code_font_size": 12.5 } });
  await settled();
  await openWorkbench();
  expect(screen.getByTestId("editor").textContent).toBe("13|false|none");
});

test("a held code buffer: leaving the Code view asks nothing and unload is prevented", async () => {
  putBuffer({
    workspaceId: CODE_ID,
    ref: "src/a.ts",
    base: { text: "a", sha256: "0".repeat(64), encoding: "utf-8", newline: "lf" },
    text: "b",
  });
  try {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount();
    await settled();
    await openWorkbench();
    await click(screen.getByRole("button", { name: "Back" }));
    expect(screen.queryByText("Workbench stub")).toBeNull();
    expect(confirm).not.toHaveBeenCalled();
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  } finally {
    dropBuffer(CODE_ID, "src/a.ts");
  }
});

test("a vault is active: the Workbench gets no active id", async () => {
  mount({ active: "vault" });
  await settled();
  await openWorkbench();
  expect(screen.getByTestId("active").textContent).toBe("none");
});

test("the code view comes back after a reload", async () => {
  window.sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "code", page: "general", panel: "locations" }),
  );
  mount();
  await settled();
  expect(screen.getByText("Workbench stub")).toBeTruthy();
});

test("without a code client the Code destination is absent and the view does not come back", async () => {
  window.sessionStorage.setItem(
    "viki.view",
    JSON.stringify({ view: "code", page: "general", panel: "locations" }),
  );
  mount({ code: false });
  await settled();
  expect(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).queryByRole(
      "button",
      { name: "Code" },
    ),
  ).toBeNull();
  expect(screen.queryByRole("tablist", { name: "Right sidebar" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Open workbench" })).toBeNull();
  expect(screen.queryByText("Workbench stub")).toBeNull();
});

test("a quote lands on its own line at the end of the draft and shows the chat", async () => {
  mount();
  await settled();
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: "look at this" } });
  await openWorkbench();
  await click(screen.getByRole("button", { name: "quote" }));
  expect(screen.queryByText("Workbench stub")).toBeNull();
  expect(draftOf()).toBe("look at this\n`src/a.ts:3-5`\n");
});

test("a quote into an empty draft adds no leading newline", async () => {
  mount();
  await settled();
  await openWorkbench();
  await click(screen.getByRole("button", { name: "quote" }));
  expect(draftOf()).toBe("`src/a.ts:3-5`\n");
});

test("open in the editor sends open.code with the line", async () => {
  const { socket } = mount();
  await settled();
  await openWorkbench();
  await click(screen.getByRole("button", { name: "open" }));
  expect(socket.sent.filter((message) => message.type === "open.code")).toEqual([
    { type: "open.code", workspace_id: FAKE_WORKSPACE_ID, path: "src/a.ts", line: 3 },
  ]);
});

test("Settings Back returns to the calling Code view with its unsaved buffer", async () => {
  putBuffer({
    workspaceId: CODE_ID,
    ref: "src/a.ts",
    base: { text: "a", sha256: "0".repeat(64), encoding: "utf-8", newline: "lf" },
    text: "b",
  });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  try {
    mount();
    await settled();
    await openWorkbench();
    await click(screen.getByRole("button", { name: "settings" }));
    expect(screen.queryByText("Workbench stub")).toBeNull();
    expect(document.getElementById("settings-tab-workspaces")?.getAttribute("aria-selected")).toBe(
      "true",
    );
    await click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText("Workbench stub")).toBeTruthy();
    expect(getBuffer(CODE_ID, "src/a.ts")?.text).toBe("b");
    expect(confirm).not.toHaveBeenCalled();
  } finally {
    dropBuffer(CODE_ID, "src/a.ts");
  }
});

test("Settings history excludes dirty refusals and a repeated current subpage", async () => {
  mount();
  await settled();
  await openWorkbench();
  await click(screen.getByRole("button", { name: "settings" }));
  const general = document.getElementById("settings-tab-general") as HTMLElement;
  await click(general);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  act(() => setUnsavedWork("routine", true));
  try {
    await click(general);
    expect(confirm).not.toHaveBeenCalled();
    await click(document.getElementById("settings-tab-privacy") as HTMLElement);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(general.getAttribute("aria-selected")).toBe("true");
    await click(screen.getByRole("button", { name: "Back" }));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(general.getAttribute("aria-selected")).toBe("true");
  } finally {
    act(() => setUnsavedWork("routine", false));
  }
  await click(screen.getByRole("button", { name: "Back" }));
  expect(document.getElementById("settings-tab-workspaces")?.getAttribute("aria-selected")).toBe(
    "true",
  );
  await click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByText("Workbench stub")).toBeTruthy();
});

test("Data Back restores the prior panel then Code without recording a refused or noop panel", async () => {
  mount();
  await settled();
  await openWorkbench();
  async function command(name: string) {
    await click(screen.getByRole("button", { name: "App menu" }));
    await click(screen.getByRole("menuitem", { name }));
  }
  await command("Data locations");
  await command("Export diagnostics");
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Export" }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  act(() => setUnsavedWork("routine", true));
  try {
    await command("Export diagnostics");
    expect(confirm).not.toHaveBeenCalled();
    await command("Import settings");
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("heading", { name: "Data" })).toBeTruthy();
  } finally {
    act(() => setUnsavedWork("routine", false));
  }
  await click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByRole("heading", { name: "Locations" })).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "App menu" }));
  const navigation = JSON.parse(window.sessionStorage.getItem("viki.view") ?? "null");
  expect(navigation).toMatchObject({ view: "data", panel: "locations" });
  await click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByText("Workbench stub")).toBeTruthy();
});

test("entering and leaving capability Recovery clears accepted page history", async () => {
  const { viki, shell } = mount();
  await settled();
  await openWorkbench();
  await click(screen.getByRole("button", { name: "settings" }));
  await click(document.getElementById("settings-tab-general") as HTMLElement);
  async function mode(value: "normal" | "recovery") {
    shell.data.capabilities.app.mode = value;
    await act(async () => {
      viki.store.setStatus("reconnecting");
      viki.store.setStatus("open");
    });
    await settled();
  }
  await mode("recovery");
  expect(screen.getByRole("heading", { name: "VIKI is running in recovery mode." })).toBeTruthy();
  await mode("normal");
  await click(screen.getByRole("button", { name: "Back" }));
  expect(document.getElementById("settings-tab-general")?.getAttribute("aria-selected")).toBe(
    "true",
  );
  expect(screen.queryByText("Workbench stub")).toBeNull();
  expect(screen.queryByLabelText("Message")).toBeNull();
});

test("with a code folder active the git chip and vault tiles are off", async () => {
  const rails: Rails = {
    ...emptyRails(),
    gates: [gate({ id: "H1", item: "Recall skill" })],
    counts: { knowledge: 4, sources: 2, inbox: 1 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "abc1234",
      committed_at: "",
      dirty: 0,
    },
  };
  // The vault window draws both, so the selectors below match something.
  const vault = mount({ active: "vault", rails });
  await settled();
  await waitFor(() => expect(vault.container.querySelector(".tile")).not.toBeNull());
  expect(vault.container.querySelector(".git-chip")).not.toBeNull();
  cleanup();

  const code = mount({ active: "code", rails });
  await settled();
  await waitFor(() => expect(code.container.querySelector(".rail-section")).not.toBeNull());
  expect(code.container.querySelector(".tile")).toBeNull();
  expect(code.container.querySelector(".git-chip")).toBeNull();
  expect(code.container.textContent).not.toContain("Needs you");
});

test("with a code folder active the Vault tab shows the code-folder placeholder", async () => {
  mount();
  await settled();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  expect(screen.getByRole("heading", { level: 1, name: "Vault" })).toBeTruthy();
  expect(
    screen.getByText("The active workspace is a code folder; it shows no notes."),
  ).toBeTruthy();
  cleanup();

  mount();
  await settled();
  // The settings load sets the language, so the English copy is switched in after it.
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  expect(
    screen.getByText("The active workspace is a code folder; it shows no notes."),
  ).toBeTruthy();
});
