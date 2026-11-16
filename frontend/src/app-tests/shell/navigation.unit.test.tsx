import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useEffect, useLayoutEffect, useRef } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { RoutineDetailData, RoutinesApi } from "../../clients/automation/routinesApi";
import type { CodeApi } from "../../clients/code/codeApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import type { RecoveryViewProps } from "../../components/Recovery/RecoveryView";
import type { WorkbenchProps } from "../../components/Workbench";
import type { RoutineNotificationOptions } from "../../state/routineNotifications";
import type { VaultApi } from "../../state/vaultApi";
import type { VaultOpsApi } from "../../state/vaultOpsApi";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import { openWorkbench } from "../../testWorkbench";

const routineNotice = vi.hoisted(() => ({ open: null as ((routineId: string) => void) | null }));
vi.mock("../../state/routineNotifications", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../state/routineNotifications")>();
  return {
    ...original,
    useRoutineNotifications: (options: RoutineNotificationOptions) => {
      routineNotice.open = options.onOpen;
      original.useRoutineNotifications(options);
    },
  };
});

const codeReturn = vi.hoisted(() => ({ mode: "normal", identity: "name" }));
vi.mock("../../components/Workbench", () => ({
  Workbench: (props: WorkbenchProps) => {
    const heading = useRef<HTMLHeadingElement>(null);
    useEffect(() => {
      heading.current?.focus();
      const outer = heading.current?.closest<HTMLElement>('[data-region="view"]');
      if (outer) {
        outer.scrollTop = 31;
        outer.scrollLeft = 7;
      }
      const inner = heading.current?.parentElement?.querySelector<HTMLElement>(
        '[data-testid="code-inner-scroll"]',
      );
      if (inner) {
        inner.scrollTop = 29;
        inner.scrollLeft = 3;
      }
    }, []);
    const control = () => (
      <button
        type="button"
        onClick={props.onOpenSettings}
        disabled={codeReturn.mode === "disabled"}
        aria-disabled={codeReturn.mode === "aria-disabled" || undefined}
        {...(codeReturn.identity !== "name" ? { [codeReturn.identity]: "code-settings" } : {})}
      >
        {codeReturn.mode === "renamed" ? "Current settings" : "Code Settings"}
      </button>
    );
    return (
      <section>
        {props.renderBack?.(props.onBack) ?? (
          <button type="button" onClick={props.onBack}>
            Standalone Code Back
          </button>
        )}
        <h1 ref={heading} tabIndex={-1}>
          Code fixture
        </h1>
        <div
          hidden={codeReturn.mode === "hidden"}
          inert={codeReturn.mode === "inert"}
          aria-hidden={codeReturn.mode === "aria-hidden" || undefined}
          style={{ display: codeReturn.mode === "css-hidden" ? "none" : undefined }}
        >
          {codeReturn.mode !== "missing" && control()}
          {codeReturn.mode === "duplicate" && control()}
        </div>
        <div data-testid="code-inner-scroll">Private editor fixture</div>
      </section>
    );
  },
}));

const earlyRecovery = vi.hoisted(() => [] as { disabled: boolean; reason: string | null }[]);
vi.mock("../../components/Recovery/RecoveryView", async (importOriginal) => {
  const { RecoveryView } =
    await importOriginal<typeof import("../../components/Recovery/RecoveryView")>();
  return {
    RecoveryView: (props: RecoveryViewProps) => {
      useLayoutEffect(() => {
        const back = screen.queryByRole<HTMLButtonElement>("button", { name: "Back" });
        earlyRecovery.push({
          disabled: back?.disabled ?? false,
          reason:
            document.getElementById(back?.getAttribute("aria-describedby") ?? "")?.textContent ??
            null,
        });
      }, []);
      return <RecoveryView {...props} />;
    },
  };
});

beforeEach(() => {
  window.sessionStorage.clear();
  earlyRecovery.length = 0;
  routineNotice.open = null;
  codeReturn.mode = "normal";
  codeReturn.identity = "name";
});
afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});
const pending = () => new Promise<never>(() => {});
async function settled() {
  await act(async () => {
    for (let n = 0; n < 5; n++) await Promise.resolve();
  });
}
const click = async (element: HTMLElement) => act(async () => void fireEvent.click(element));
const back = () => screen.getByRole<HTMLButtonElement>("button", { name: "Back" });
function mount(
  options: {
    peers?: boolean;
    choose?: boolean;
    items?: RoutineDetailData[];
    vault?: VaultApi;
    ops?: VaultOpsApi;
  } = {},
) {
  const fake = fakeViki();
  const workspace = workspaceFixture({ root_physical_id: "12:34" });
  const shell = fakeShellApi({
    settings: settingsFixture({ "onboarding.completed": options.choose !== true }),
    ...(options.peers
      ? {
          workspaces: {
            ...listingFixture([workspace]),
            active_id: workspace.workspace_id,
            chat_only: false,
          },
          capabilities: capabilitiesFixture({
            workspace: {
              shared_scratch: false,
              chat_only: false,
              active: {
                id: workspace.workspace_id,
                kind: workspace.kind,
                name: workspace.name,
                policy: workspace.policy,
                layout: "unknown",
                source: "created",
                aliased: false,
                root_available: true,
              },
            },
          }),
        }
      : {}),
  });
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
  const routines = {
    list: async () => ({
      routines: options.items ?? [],
      scheduler: { state: "running", attended: true, paused: false },
    }),
    get: vi.fn(async (id: string) => {
      if (!options.items) return pending();
      const detail = options.items.find((item) => item.routine_id === id);
      if (!detail) throw new Error("missing fixture routine");
      return detail;
    }),
    runs: vi.fn(options.items ? async () => [] : pending),
    preview: pending,
    create: vi.fn(pending),
    revise: vi.fn(pending),
    setState: vi.fn(pending),
    run: vi.fn(pending),
  } satisfies RoutinesApi;
  const code: CodeApi = {
    tree: pending,
    file: pending,
    search: pending,
    changes: pending,
    diff: pending,
    save: pending,
  };
  render(
    <App
      viki={fake.viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shell}
      codeApi={code}
      routinesApi={routines}
      {...(options.ops ? { vaultOpsApi: options.ops } : {})}
      {...(options.peers ? { profileApi: profiles, vaultApi: options.vault ?? vault } : {})}
    />,
  );
  act(() => fake.socket.open());
  return { ...fake, shell, routines };
}

test("one page Back restores Code through actual Settings history without recording a noop", async () => {
  mount();
  await settled();
  expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
  await openWorkbench();
  await click(screen.getByRole("button", { name: "Code Settings" }));
  expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
  await click(screen.getByRole("tab", { name: "General" }));
  await click(screen.getByRole("tab", { name: "General" }));
  await click(back());
  expect(document.getElementById("settings-tab-workspaces")?.getAttribute("aria-selected")).toBe(
    "true",
  );
  await click(back());
  expect(screen.getByRole("heading", { name: "Code fixture" })).toBeTruthy();
  expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
});

test("actual Routine dirty Back declines intact then admits once to its prior Code page", async () => {
  const { routines } = mount();
  await settled();
  await openWorkbench();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Routines" },
    ),
  );
  await click(screen.getByRole("button", { name: "New routine" }));
  const name = screen.getByLabelText<HTMLInputElement>("Name");
  await act(async () => void fireEvent.change(name, { target: { value: "Draft" } }));
  const control =
    screen.queryByRole<HTMLButtonElement>("button", { name: "Back" }) ??
    screen.getByRole<HTMLButtonElement>("button", { name: "Back to the list" });
  control.focus();
  const outer = control.closest<HTMLElement>('[data-region="view"]');
  if (!outer) throw new Error("Missing current outer view");
  outer.scrollTop = 64;
  outer.scrollLeft = 9;
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await click(control);
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("Name")).toBe(name);
  expect(name.value).toBe("Draft");
  expect(document.activeElement).toBe(control);
  expect([outer.scrollTop, outer.scrollLeft]).toEqual([64, 9]);
  confirm.mockClear().mockReturnValue(true);
  await click(control);
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("heading", { name: "Code fixture" })).toBeTruthy();
  for (const action of [routines.create, routines.revise, routines.setState, routines.run])
    expect(action).not.toHaveBeenCalled();
  await click(back());
  expect(screen.getByLabelText("Message")).toBeTruthy();
});

test("fresh page and derived Recovery expose disabled Back with an honest current reason", async () => {
  window.sessionStorage.setItem("viki.view", JSON.stringify({ view: "code" }));
  const { viki, shell } = mount();
  await settled();
  expect(back().disabled).toBe(true);
  expect(document.getElementById(back().getAttribute("aria-describedby") ?? "")?.textContent).toBe(
    "No earlier page in this window.",
  );
  await click(screen.getByRole("button", { name: "Code Settings" }));
  shell.data.capabilities.app.mode = "recovery";
  await act(async () => {
    viki.store.setStatus("reconnecting");
    viki.store.setStatus("open");
  });
  await settled();
  expect(earlyRecovery[0]).toEqual({
    disabled: true,
    reason: "Back is unavailable in Recovery mode.",
  });
  expect(back().disabled).toBe(true);
  await click(back());
  expect(screen.getByRole("heading", { name: "VIKI is running in recovery mode." })).toBeTruthy();
  shell.data.capabilities.app.mode = "normal";
  await act(async () => {
    viki.store.setStatus("reconnecting");
    viki.store.setStatus("open");
  });
  await settled();
  expect(back().disabled).toBe(true);
  expect(document.getElementById(back().getAttribute("aria-describedby") ?? "")?.textContent).toBe(
    "No earlier page in this window.",
  );
  await click(back());
  expect(screen.queryByRole("heading", { name: "Code fixture" })).toBeNull();
});

test.each(["Agents", "Vault"])(
  "%s has one page Back and restores the actual prior Settings page",
  async (tab) => {
    mount({ peers: true });
    await settled();
    await openWorkbench();
    await click(screen.getByRole("button", { name: "Code Settings" }));
    await click(
      within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
        "button",
        { name: tab },
      ),
    );
    expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
    await click(back());
    expect(document.getElementById("settings-tab-workspaces")?.getAttribute("aria-selected")).toBe(
      "true",
    );
  },
);

test("Data's actual page callback restores Settings through one Back", async () => {
  mount();
  await settled();
  await openWorkbench();
  await click(screen.getByRole("button", { name: "Code Settings" }));
  await click(screen.getByRole("button", { name: "App menu" }));
  await click(screen.getByRole("menuitem", { name: "Data locations" }));
  expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
  await click(back());
  expect(document.getElementById("settings-tab-workspaces")?.getAttribute("aria-selected")).toBe(
    "true",
  );
});

test("onboarding chooser owns history Back while its create form keeps only local Back", async () => {
  mount({ choose: true });
  await settled();
  expect(back().disabled).toBe(true);
  expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
  await click(screen.getByRole("button", { name: /^Create a vault/ }));
  expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
  expect(back().getAttribute("aria-describedby")).toBeNull();
  expect(screen.queryByText("No earlier page in this window.")).toBeNull();
  await click(back());
  expect(screen.getByRole("button", { name: /^Create a vault/ })).toBeTruthy();
  expect(back().disabled).toBe(true);
  expect(screen.queryByLabelText("Message")).toBeNull();
});

test.each(["name", "id", "data-page", "data-region"])(
  "Back resolves a remounted %s opener after child entry focus and restores only outer offsets",
  async (identity) => {
    codeReturn.identity = identity;
    mount();
    await settled();
    await openWorkbench();
    const opener = screen.getByRole("button", { name: "Code Settings" });
    const outer = opener.closest<HTMLElement>('[data-region="view"]');
    if (!outer) throw new Error("Missing current outer view");
    outer.scrollTop = 84;
    outer.scrollLeft = 9;
    opener.focus();
    await click(opener);
    expect(opener.isConnected).toBe(false);
    if (identity !== "name") codeReturn.mode = "renamed";
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    await click(back());
    const returned = screen.getByRole("button", {
      name: identity === "name" ? "Code Settings" : "Current settings",
    });
    expect(returned).not.toBe(opener);
    expect(document.activeElement).toBe(returned);
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    const currentOuter = returned.closest<HTMLElement>('[data-region="view"]');
    if (!currentOuter) throw new Error("Missing returned outer view");
    expect([currentOuter.scrollTop, currentOuter.scrollLeft]).toEqual([84, 9]);
    const inner = screen.getByTestId("code-inner-scroll");
    expect([inner.scrollTop, inner.scrollLeft]).toEqual([29, 3]);
  },
);

test.each([
  "missing",
  "disabled",
  "aria-disabled",
  "duplicate",
  "hidden",
  "inert",
  "aria-hidden",
  "css-hidden",
])("Back falls back to the current heading for a %s opener", async (mode) => {
  mount();
  await settled();
  await openWorkbench();
  const opener = screen.getByRole("button", { name: "Code Settings" });
  opener.focus();
  await click(opener);
  codeReturn.mode = mode;
  await click(back());
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Code fixture" }));
});

test("Back prefers a connected Settings opener over the page entry heading", async () => {
  mount();
  await settled();
  await openWorkbench();
  await click(screen.getByRole("button", { name: "Code Settings" }));
  const general = screen.getByRole("tab", { name: "General" });
  general.focus();
  await click(general);
  await click(back());
  expect(document.activeElement).toBe(general);
  expect(document.getElementById("settings-tab-workspaces")?.getAttribute("aria-selected")).toBe(
    "true",
  );
});

test("a later accepted forward transition cancels obsolete Back restoration", async () => {
  mount({ peers: true });
  await settled();
  await openWorkbench();
  const opener = screen.getByRole("button", { name: "Code Settings" });
  opener.focus();
  await click(opener);
  const obsoleteFocus = vi.spyOn(HTMLElement.prototype, "focus");
  await act(async () => {
    fireEvent.click(back());
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
        "button",
        { name: "Agents" },
      ),
    );
  });
  expect(screen.queryByRole("heading", { name: "Code fixture" })).toBeNull();
  expect(
    obsoleteFocus.mock.contexts.some(
      (node) =>
        node instanceof HTMLElement &&
        ["Code Settings", "Code fixture"].includes(node.textContent ?? ""),
    ),
  ).toBe(false);
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Agents" }));
});
async function escapeSettings() {
  mount();
  await settled();
  await openWorkbench();
  const opener = screen.getByRole("button", { name: "Code Settings" });
  opener.focus();
  await click(opener);
  back().focus();
  return back();
}
async function pageEscape(
  target: Window | HTMLElement = window,
  init: KeyboardEventInit = {},
  consumed = false,
) {
  const event = new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    key: "Escape",
    ...init,
  });
  if (consumed) event.preventDefault();
  await act(async () => void target.dispatchEvent(event));
  return event;
}

test("page Escape clicks the mounted Back once and restores its actual Code opener", async () => {
  const control = await escapeSettings();
  const nativeClick = vi.spyOn(control, "click");
  const event = await pageEscape(control);
  expect(nativeClick).toHaveBeenCalledTimes(1);
  expect(event.defaultPrevented).toBe(true);
  expect(screen.getByRole("heading", { name: "Code fixture" })).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Code Settings" }));
  await pageEscape(back());
  expect(screen.getByLabelText("Message")).toBeTruthy();
});

test("actual Routine Escape preserves a declined draft then admits once through leaf Back", async () => {
  const { routines } = mount();
  await settled();
  await openWorkbench();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Routines" },
    ),
  );
  await click(screen.getByRole("button", { name: "New routine" }));
  const name = screen.getByLabelText<HTMLInputElement>("Name");
  await act(async () => void fireEvent.change(name, { target: { value: "Escape draft" } }));
  const control = back();
  control.focus();
  const outer = control.closest<HTMLElement>('[data-region="view"]');
  if (!outer) throw new Error("Missing current Routine outer view");
  outer.scrollTop = 64;
  outer.scrollLeft = 9;
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  expect((await pageEscape(control)).defaultPrevented).toBe(true);
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(confirm).toHaveBeenCalledWith("Discard unsaved changes?");
  expect(screen.getByLabelText("Name")).toBe(name);
  expect(name.value).toBe("Escape draft");
  expect(document.activeElement).toBe(control);
  expect([outer.scrollTop, outer.scrollLeft]).toEqual([64, 9]);
  confirm.mockClear().mockReturnValue(true);
  await pageEscape(control);
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(confirm).toHaveBeenCalledWith("Discard unsaved changes?");
  expect(screen.getByRole("heading", { name: "Code fixture" })).toBeTruthy();
  for (const action of [routines.create, routines.revise, routines.setState, routines.run])
    expect(action).not.toHaveBeenCalled();
  await click(back());
  expect(screen.getByLabelText("Message")).toBeTruthy();
});

test.each([
  ["composition", { isComposing: true }, false],
  ["229", { keyCode: 229 }, false],
  ["Control", { ctrlKey: true }, false],
  ["Alt", { altKey: true }, false],
  ["Meta", { metaKey: true }, false],
  ["Shift", { shiftKey: true }, false],
  ["consumed", {}, true],
] as const)("%s Escape leaves the current page and history intact", async (_, init, consumed) => {
  const control = await escapeSettings();
  const nativeClick = vi.spyOn(control, "click");
  await pageEscape(control, init, consumed);
  expect(nativeClick).not.toHaveBeenCalled();
  expect(
    screen.getByRole("tab", { name: "Workspaces and Vault" }).getAttribute("aria-selected"),
  ).toBe("true");
  await pageEscape(control);
  expect(nativeClick).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("heading", { name: "Code fixture" })).toBeTruthy();
});

test.each(["input", "textarea", "select", "contenteditable", "code-editor"])(
  "Escape belongs to the focused %s rather than page Back",
  async (kind) => {
    const control = await escapeSettings();
    const nativeClick = vi.spyOn(control, "click");
    const host = document.createElement("div");
    const field = document.createElement(
      kind === "contenteditable" ? "div" : kind === "code-editor" ? "button" : kind,
    );
    if (kind === "contenteditable") {
      field.setAttribute("contenteditable", "true");
      Object.defineProperty(field, "isContentEditable", { value: true });
    }
    if (kind === "code-editor") host.className = "code-editor";
    field.tabIndex = 0;
    host.append(field);
    control.parentElement?.append(host);
    field.focus();
    await pageEscape(field);
    expect(nativeClick).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(field);
    expect(
      screen.getByRole("tab", { name: "Workspaces and Vault" }).getAttribute("aria-selected"),
    ).toBe("true");
    host.remove();
    control.focus();
    await pageEscape();
    expect(screen.getByRole("heading", { name: "Code fixture" })).toBeTruthy();
  },
);

test("a same-turn accepted subpage excludes its old mounted Back from Escape", async () => {
  const control = await escapeSettings();
  const nativeClick = vi.spyOn(control, "click");
  await act(async () => {
    fireEvent.click(screen.getByRole("tab", { name: "General" }));
    fireEvent.keyDown(control, { key: "Escape" });
  });
  expect(nativeClick).not.toHaveBeenCalled();
  expect(screen.getByRole("tab", { name: "General" }).getAttribute("aria-selected")).toBe("true");
  await click(back());
  expect(
    screen.getByRole("tab", { name: "Workspaces and Vault" }).getAttribute("aria-selected"),
  ).toBe("true");
  await click(back());
  expect(screen.getByRole("heading", { name: "Code fixture" })).toBeTruthy();
});

test("root and current Recovery Back do not admit page Escape", async () => {
  window.sessionStorage.setItem("viki.view", JSON.stringify({ view: "code" }));
  const { viki, shell } = mount();
  await settled();
  const root = back();
  const rootClick = vi.spyOn(root, "click");
  await pageEscape(root);
  expect(rootClick).not.toHaveBeenCalled();
  expect(screen.getByRole("heading", { name: "Code fixture" })).toBeTruthy();
  await click(screen.getByRole("button", { name: "Code Settings" }));
  shell.data.capabilities.app.mode = "recovery";
  await act(async () => {
    viki.store.setStatus("reconnecting");
    viki.store.setStatus("open");
  });
  await settled();
  const recoveryClick = vi.spyOn(back(), "click");
  await pageEscape(back());
  expect(recoveryClick).not.toHaveBeenCalled();
  expect(screen.getByRole("heading", { name: "VIKI is running in recovery mode." })).toBeTruthy();
});

test("actual app menu and modal own Escape before the page Back", async () => {
  const nativeClick = vi.spyOn(await escapeSettings(), "click");
  await click(screen.getByRole("button", { name: "App menu" }));
  expect(screen.getByRole("menu")).toBeTruthy();
  await pageEscape();
  expect(screen.queryByRole("menu")).toBeNull();
  await act(async () => void fireEvent.keyDown(window, { key: "k", ctrlKey: true }));
  expect(screen.getByRole("dialog")).toBeTruthy();
  await pageEscape();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(nativeClick).not.toHaveBeenCalled();
  expect(
    screen.getByRole("tab", { name: "Workspaces and Vault" }).getAttribute("aria-selected"),
  ).toBe("true");
});

test("onboarding form Escape returns only to its local chooser", async () => {
  mount({ choose: true });
  await settled();
  await click(screen.getByRole("button", { name: /^Create a vault/ }));
  await pageEscape();
  expect(screen.getByRole("button", { name: /^Create a vault/ })).toBeTruthy();
  expect(back().disabled).toBe(true);
  expect(screen.queryByLabelText("Message")).toBeNull();
});

function routineContext(id: string): RoutineDetailData {
  return {
    routine_id: id,
    name: id,
    state: "draft",
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: "manual", timezone: null },
    next_run: null,
    last_run: null,
    last_attempt: null,
    active_run: null,
    next_five: [],
    readiness: { blockers: [], run_blockers: [], warnings: [] },
    resolved: { profile: null, workspace: null, first_turn: null, deadline_minutes: 30 },
    document: {
      name: id,
      purpose: "",
      prompt: "Synthetic routine",
      profile_id: "p1",
      workspace_id: "w1",
      deadline_minutes: 30,
      schedule: {
        kind: "manual",
        times: [],
        days: [],
        timezone: null,
        start_date: null,
        end_date: null,
        catch_up: "skip",
      },
    },
    tested_revision: null,
    activates_at: "",
  };
}
const contextItems = () => [routineContext("Routine/A"), routineContext("routine/a")];
const routineRow = (id: string) => screen.getByRole("button", { name: id });
async function routineHistory() {
  const fixture = mount({ items: contextItems() });
  await settled();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Routines" },
    ),
  );
  return fixture;
}

test("same-turn literal Routine IDs record two details but no same-ID duplicate", async () => {
  const { routines } = await routineHistory();
  act(() => {
    fireEvent.click(routineRow("Routine/A"));
    fireEvent.click(routineRow("Routine/A"));
    fireEvent.click(routineRow("routine/a"));
  });
  await settled();
  expect(screen.getByRole("heading", { name: "routine/a" })).toBeTruthy();
  await click(back());
  expect(screen.getByRole("heading", { name: "Routine/A" })).toBeTruthy();
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBe("true");
  await click(back());
  expect(screen.queryByRole("heading", { name: "Routine/A" })).toBeNull();
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBeNull();
  expect(routines.get.mock.calls.map(([id]) => id)).toEqual(["routine/a", "Routine/A"]);
  await click(back());
  expect(screen.getByRole("textbox", { name: "Message" })).toBeTruthy();
  expect(routines.create).not.toHaveBeenCalled();
  expect(routines.revise).not.toHaveBeenCalled();
  expect(routines.setState).not.toHaveBeenCalled();
  expect(routines.run).not.toHaveBeenCalled();
});

test("Routine detail through Settings Back restores the exact reader ID", async () => {
  const { routines } = await routineHistory();
  await click(routineRow("Routine/A"));
  await act(async () => void fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
  expect(screen.getByRole("tab", { name: "Workspaces and Vault" })).toBeTruthy();
  await click(back());
  expect(screen.getByRole("heading", { name: "Routine/A" })).toBeTruthy();
  expect(routines.get.mock.calls.map(([id]) => id)).toEqual(["Routine/A", "Routine/A"]);
  await click(back());
  expect(screen.queryByRole("heading", { name: "Routine/A" })).toBeNull();
});

const noRoutineWrites = (routines: ReturnType<typeof mount>["routines"]) => {
  for (const action of [routines.create, routines.revise, routines.setState, routines.run])
    expect(action).not.toHaveBeenCalled();
};
async function dirtyRoutine() {
  await click(screen.getByRole("button", { name: "Edit" }));
  const name = screen.getByLabelText<HTMLInputElement>("Name");
  await act(async () => void fireEvent.change(name, { target: { value: "Draft" } }));
  return name;
}

test("declined dirty Routine row selection retains its exact draft, focus, offsets and history", async () => {
  const { routines } = await routineHistory();
  await click(routineRow("Routine/A"));
  const name = await dirtyRoutine();
  name.focus();
  const outer = name.closest<HTMLElement>('[data-region="view"]');
  if (!outer) throw new Error("Missing current outer view");
  outer.scrollTop = 72;
  outer.scrollLeft = 5;
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await click(routineRow("routine/a"));
  expect(screen.getByLabelText("Name")).toBe(name);
  expect(name.value).toBe("Draft");
  expect(document.activeElement).toBe(name);
  expect([outer.scrollTop, outer.scrollLeft]).toEqual([72, 5]);
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBe("true");
  expect(routines.get.mock.calls.map(([id]) => id)).toEqual(["Routine/A"]);
  expect(confirm).toHaveBeenCalledExactlyOnceWith("Discard unsaved changes?");
  confirm.mockClear().mockReturnValue(true);
  await click(routineRow("routine/a"));
  expect(confirm).toHaveBeenCalledExactlyOnceWith("Discard unsaved changes?");
  expect(screen.queryByLabelText("Name")).toBeNull();
  expect(screen.getByRole("heading", { name: "routine/a" })).toBeTruthy();
  await click(back());
  expect(screen.getByRole("heading", { name: "Routine/A" })).toBeTruthy();
  await click(back());
  expect(screen.queryByRole("heading", { name: "Routine/A" })).toBeNull();
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBeNull();
  expect(confirm).toHaveBeenCalledTimes(1);
  noRoutineWrites(routines);
});

test("dirty same-view Routine Back declines intact then restores the prior reader once", async () => {
  const { routines } = await routineHistory();
  await click(routineRow("Routine/A"));
  await click(routineRow("routine/a"));
  const name = await dirtyRoutine();
  const control = back();
  control.focus();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await click(control);
  expect(screen.getByLabelText("Name")).toBe(name);
  expect(name.value).toBe("Draft");
  expect(document.activeElement).toBe(control);
  expect(confirm).toHaveBeenCalledExactlyOnceWith("Discard unsaved changes?");
  confirm.mockClear().mockReturnValue(true);
  await click(control);
  expect(confirm).toHaveBeenCalledExactlyOnceWith("Discard unsaved changes?");
  expect(screen.queryByLabelText("Name")).toBeNull();
  expect(screen.getByRole("heading", { name: "Routine/A" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "routine/a" })).toBeNull();
  expect(routines.get.mock.calls.map(([id]) => id)).toEqual([
    "Routine/A",
    "routine/a",
    "Routine/A",
  ]);
  await click(back());
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBeNull();
  expect(confirm).toHaveBeenCalledTimes(1);
  noRoutineWrites(routines);
});

test("Routine notification callback admission declines without a step and accepts one global confirmation", async () => {
  const { routines } = await routineHistory();
  await click(routineRow("Routine/A"));
  const name = await dirtyRoutine();
  name.focus();
  const open = routineNotice.open;
  if (!open) throw new Error("Missing App Routine notification callback");
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  await act(async () => void open("routine/a"));
  expect(screen.getByLabelText("Name")).toBe(name);
  expect(name.value).toBe("Draft");
  expect(document.activeElement).toBe(name);
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBe("true");
  expect(routines.get.mock.calls.map(([id]) => id)).toEqual(["Routine/A"]);
  expect(confirm).toHaveBeenCalledExactlyOnceWith(
    "Your unsaved changes will be lost. Leave this view?",
  );
  confirm.mockClear().mockReturnValue(true);
  await act(async () => void open("routine/a"));
  expect(confirm).toHaveBeenCalledExactlyOnceWith(
    "Your unsaved changes will be lost. Leave this view?",
  );
  expect(screen.queryByLabelText("Name")).toBeNull();
  expect(screen.getByRole("heading", { name: "routine/a" })).toBeTruthy();
  await click(back());
  expect(screen.getByRole("heading", { name: "Routine/A" })).toBeTruthy();
  await click(back());
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBeNull();
  expect(confirm).toHaveBeenCalledTimes(1);
  noRoutineWrites(routines);
});

test("a failed restored Routine ID uses its existing Retry reader without relinking or actions", async () => {
  const { routines } = await routineHistory();
  await click(routineRow("Routine/A"));
  await click(routineRow("routine/a"));
  routines.get.mockRejectedValueOnce(new Error("Synthetic unavailable routine"));
  await click(back());
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBe("true");
  expect(screen.queryByRole("heading", { name: "Routine/A" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "routine/a" })).toBeNull();
  expect(screen.getByRole("alert").textContent).toBe("The action could not be completed.");
  expect(routines.get.mock.calls.map(([id]) => id)).toEqual([
    "Routine/A",
    "routine/a",
    "Routine/A",
  ]);
  await click(screen.getByRole("button", { name: "Retry" }));
  expect(screen.getByRole("heading", { name: "Routine/A" })).toBeTruthy();
  expect(routines.get.mock.calls.map(([id]) => id)).toEqual([
    "Routine/A",
    "routine/a",
    "Routine/A",
    "Routine/A",
  ]);
  await click(back());
  expect(routineRow("Routine/A").getAttribute("aria-current")).toBeNull();
  noRoutineWrites(routines);
});

function noteNavigationApi(): VaultApi {
  const complete = (body: Record<string, unknown>) => ({
    ...body,
    partial: false,
    partial_reasons: [],
    omitted: 0,
  });
  return {
    entries: vi.fn(async () =>
      complete({
        entries: [
          { ref: "alpha/a.md", title: "A" },
          { ref: "alpha/b.md", title: "B" },
        ],
      }),
    ),
    note: vi.fn(async (_id, ref) =>
      complete({
        note: {
          ref,
          title: ref,
          text: `fresh ${ref}`,
          source: `fresh ${ref}`,
          sha256: "a".repeat(64),
          frontmatter_error: null,
        },
        resolved_links: [],
      }),
    ),
    backlinks: vi.fn(async () => complete({ backlinks: [] })),
    history: vi.fn(async () => complete({ commits: [], state: "ready" })),
    bookmarks: vi.fn(async () => ({
      bookmarks: ["alpha/a.md", "alpha/b.md"].map((ref) => ({
        ref,
        created_at: "2026-10-02T07:26:33.001Z",
      })),
    })),
    addBookmark: vi.fn(pending),
    removeBookmark: vi.fn(pending),
    createNote: vi.fn(pending),
    saveNote: vi.fn(pending),
    recovery: async () => [],
  };
}
async function vaultListing() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  await settled();
}
const noteRow = (name: string) =>
  within(screen.getByRole("navigation", { name: "Notes" })).getByRole("button", { name });
test("one H05 stack restores B across Settings, then A and the original Vault list", async () => {
  vi.useFakeTimers();
  try {
    const vault = noteNavigationApi();
    mount({ peers: true, vault });
    await settled();
    await click(
      within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
        "button",
        { name: "Vault" },
      ),
    );
    await vaultListing();
    fireEvent.change(screen.getByRole("combobox", { name: /Folder/ }), {
      target: { value: "alpha" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /Sort/ }), {
      target: { value: "recent" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Bookmarks only/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search note contents" }), {
      target: { value: "match" },
    });
    await vaultListing();
    const outer = screen
      .getByRole("region", { name: "Vault browser" })
      .closest<HTMLElement>('[data-region="view"]');
    if (!outer) throw new Error("Missing Vault outer region");
    outer.scrollTop = 189;
    outer.scrollLeft = 8;
    await click(noteRow("A"));
    await click(noteRow("B"));
    await click(noteRow("B"));
    expect(vault.note).toHaveBeenCalledTimes(2);
    expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Back to notes" })).toBeNull();
    await act(async () => void fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
    expect(screen.queryByRole("region", { name: "Vault browser" })).toBeNull();
    await click(back());
    await vaultListing();
    expect(screen.getByRole("heading", { name: "alpha/b.md" })).toBeTruthy();
    expect(vault.note).toHaveBeenCalledTimes(3);
    await click(back());
    await vaultListing();
    expect(screen.getByRole("heading", { name: "alpha/a.md" })).toBeTruthy();
    expect(vault.note).toHaveBeenCalledTimes(4);
    await click(back());
    await vaultListing();
    expect(screen.queryByRole("article", { name: "Selected note" })).toBeNull();
    expect(document.activeElement).toBe(noteRow("A"));
    const restoredOuter = noteRow("A").closest<HTMLElement>('[data-region="view"]');
    expect([restoredOuter?.scrollTop, restoredOuter?.scrollLeft]).toEqual([189, 8]);
    expect(
      (screen.getByRole("textbox", { name: "Search note contents" }) as HTMLInputElement).value,
    ).toBe("match");
    await click(back());
    expect(screen.getByRole("textbox", { name: "Message" })).toBeTruthy();
    const id = workspaceFixture().workspace_id;
    expect(
      vi.mocked(vault.note).mock.calls.map(([workspaceId, ref]) => [workspaceId, ref]),
    ).toEqual([
      [id, "alpha/a.md"],
      [id, "alpha/b.md"],
      [id, "alpha/b.md"],
      [id, "alpha/a.md"],
    ]);
    expect(vault.saveNote).not.toHaveBeenCalled();
    expect(vault.createNote).not.toHaveBeenCalled();
    expect(vault.addBookmark).not.toHaveBeenCalled();
    expect(vault.removeBookmark).not.toHaveBeenCalled();
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});

const noteSettings = async () =>
  act(async () => void fireEvent.keyDown(window, { key: ",", ctrlKey: true }));
const noNoteWrites = (vault: VaultApi) => {
  expect(vault.saveNote).not.toHaveBeenCalled();
  expect(vault.createNote).not.toHaveBeenCalled();
  expect(vault.addBookmark).not.toHaveBeenCalled();
  expect(vault.removeBookmark).not.toHaveBeenCalled();
};
async function noteHistory(vault: VaultApi) {
  mount({ peers: true, vault });
  await settled();
  await click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Vault" },
    ),
  );
  await vaultListing();
  await click(noteRow("A"));
  await click(noteRow("B"));
}
test("Vault global Back and Settings decline the same draft, then Back admits once", async () => {
  vi.useFakeTimers();
  try {
    const vault = noteNavigationApi();
    await noteHistory(vault);
    await click(screen.getByRole("button", { name: "Edit note" }));
    const editor = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Markdown" });
    fireEvent.change(editor, { target: { value: "held literal B" } });
    editor.focus();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await click(back());
    await noteSettings();
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(confirm).toHaveBeenLastCalledWith("Your unsaved changes will be lost. Leave this view?");
    expect(screen.getByRole("textbox", { name: "Markdown" })).toBe(editor);
    expect(editor.value).toBe("held literal B");
    expect(document.activeElement).toBe(editor);
    expect(vault.note).toHaveBeenCalledTimes(2);
    noNoteWrites(vault);
    confirm.mockClear().mockReturnValue(true);
    await click(back());
    await vaultListing();
    expect(confirm).toHaveBeenCalledExactlyOnceWith(
      "Your unsaved changes will be lost. Leave this view?",
    );
    expect(screen.getByRole("heading", { name: "alpha/a.md" })).toBeTruthy();
    await click(back());
    await vaultListing();
    expect(screen.queryByRole("article", { name: "Selected note" })).toBeNull();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(vault.note).toHaveBeenCalledTimes(3);
    noNoteWrites(vault);
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});
test("a pending Vault save blocks App unmount and Back even after editor Cancel", async () => {
  vi.useFakeTimers();
  try {
    const vault = noteNavigationApi();
    let finish: ((value: Awaited<ReturnType<VaultApi["saveNote"]>>) => void) | undefined;
    vi.mocked(vault.saveNote).mockImplementation(
      () =>
        new Promise<Awaited<ReturnType<VaultApi["saveNote"]>>>((resolve) => {
          finish = resolve;
        }),
    );
    await noteHistory(vault);
    await click(screen.getByRole("button", { name: "Edit note" }));
    const editor = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Markdown" });
    fireEvent.change(editor, { target: { value: "pending B" } });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await click(screen.getByRole("button", { name: "Save note" }));
    await noteSettings();
    await click(back());
    expect(screen.getByRole("textbox", { name: "Markdown" })).toBe(editor);
    expect(editor.value).toBe("pending B");
    await click(screen.getByRole("button", { name: "Cancel" }));
    await noteSettings();
    await click(back());
    expect(screen.getByRole("heading", { name: "alpha/b.md" })).toBeTruthy();
    expect(vault.note).toHaveBeenCalledTimes(2);
    expect(confirm).not.toHaveBeenCalled();
    if (!finish) throw new Error("Missing actual pending save");
    await act(async () =>
      finish?.({
        operation_id: workspaceFixture().workspace_id,
        status: "saved",
        cleanup_pending: false,
      }),
    );
    await click(back());
    await vaultListing();
    expect(screen.getByRole("heading", { name: "alpha/a.md" })).toBeTruthy();
    expect(vault.saveNote).toHaveBeenCalledTimes(1);
    expect(vault.createNote).not.toHaveBeenCalled();
    expect(vault.note).toHaveBeenCalledTimes(3);
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});
test("Vault restores saved folder, sort and bookmark browsing mode after a search and unmount", async () => {
  vi.useFakeTimers();
  try {
    const vault = noteNavigationApi();
    mount({ peers: true, vault });
    await settled();
    await click(
      within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
        "button",
        { name: "Vault" },
      ),
    );
    await vaultListing();
    fireEvent.change(screen.getByRole("combobox", { name: /Folder/ }), {
      target: { value: "alpha" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /Sort/ }), {
      target: { value: "recent" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Bookmarks only/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search note contents" }), {
      target: { value: "literal search" },
    });
    await vaultListing();
    await click(noteRow("A"));
    await noteSettings();
    await click(back());
    await vaultListing();
    await click(back());
    await vaultListing();
    expect(
      (screen.getByRole("textbox", { name: "Search note contents" }) as HTMLInputElement).value,
    ).toBe("literal search");
    expect(document.activeElement).toBe(noteRow("A"));
    fireEvent.change(screen.getByRole("textbox", { name: "Search note contents" }), {
      target: { value: "" },
    });
    await vaultListing();
    expect((screen.getByRole("combobox", { name: /Folder/ }) as HTMLSelectElement).value).toBe(
      "alpha",
    );
    expect((screen.getByRole("combobox", { name: /Sort/ }) as HTMLSelectElement).value).toBe(
      "recent",
    );
    expect(
      (screen.getByRole("checkbox", { name: /Bookmarks only/ }) as HTMLInputElement).checked,
    ).toBe(true);

    expect(vault.note).toHaveBeenCalledTimes(2);
    noNoteWrites(vault);
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});

test.each(["row", "link", "backlink"])(
  "Vault %s records literal case-sensitive refs in the existing App history",
  async (kind) => {
    vi.useFakeTimers();
    try {
      const vault = noteNavigationApi();
      const result = (body: Record<string, unknown>) => ({
        ...body,
        partial: false,
        partial_reasons: [],
        omitted: 0,
      });
      vi.mocked(vault.entries).mockResolvedValue(
        result({
          entries: [
            { ref: "Case/A.md", title: "Upper" },
            { ref: "case/a.md", title: "Lower" },
          ],
        }),
      );
      vi.mocked(vault.note).mockImplementation(async (_id, ref) =>
        result({
          note: {
            ref,
            title: ref,
            text: `literal ${ref}`,
            source: `literal ${ref}`,
            sha256: "a".repeat(64),
            frontmatter_error: null,
          },
          resolved_links: [{ target: "lower", state: "resolved", candidates: ["case/a.md"] }],
        }),
      );
      vi.mocked(vault.backlinks).mockResolvedValue(
        result({ backlinks: [{ source: "case/a.md", line: 1 }] }),
      );
      mount({ peers: true, vault });
      await settled();
      await click(
        within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
          "button",
          { name: "Vault" },
        ),
      );
      await vaultListing();
      await click(noteRow("Upper"));
      const target =
        kind === "row"
          ? noteRow("Lower")
          : within(
              screen.getByRole("region", { name: kind === "link" ? "Links" : "Backlinks" }),
            ).getByRole("button");
      target.focus();
      const outer = target.closest<HTMLElement>('[data-region="view"]');
      if (!outer) throw new Error("Missing note outer region");
      outer.scrollTop = 127;
      await click(target);
      expect(screen.getByRole("heading", { name: "case/a.md" })).toBeTruthy();
      expect(screen.getAllByRole("button", { name: "Back" })).toHaveLength(1);
      await click(back());
      await vaultListing();
      expect(screen.getByRole("heading", { name: "Case/A.md" })).toBeTruthy();
      const restored =
        kind === "row"
          ? noteRow("Lower")
          : within(
              screen.getByRole("region", { name: kind === "link" ? "Links" : "Backlinks" }),
            ).getByRole("button");
      expect(document.activeElement).toBe(restored);
      expect(restored.closest<HTMLElement>('[data-region="view"]')?.scrollTop).toBe(127);
      expect(vi.mocked(vault.note).mock.calls.map(([id, ref]) => [id, ref])).toEqual(
        ["Case/A.md", "case/a.md", "Case/A.md"].map((ref) => [
          workspaceFixture().workspace_id,
          ref,
        ]),
      );
      await click(back());
      await vaultListing();
      expect(screen.queryByRole("article", { name: "Selected note" })).toBeNull();
      noNoteWrites(vault);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  },
);
test("Vault modal owns Escape and prevents a competing Settings transition", async () => {
  vi.useFakeTimers();
  try {
    const vault = noteNavigationApi();
    await noteHistory(vault);
    const confirm = vi.spyOn(window, "confirm");
    await act(async () => void fireEvent.keyDown(window, { key: "k", ctrlKey: true }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    await noteSettings();
    expect(screen.getByRole("dialog")).toBeTruthy();
    await pageEscape();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("heading", { name: "alpha/b.md" })).toBeTruthy();
    expect(vault.note).toHaveBeenCalledTimes(2);
    expect(confirm).not.toHaveBeenCalled();
    back().focus();
    await pageEscape();
    await vaultListing();
    expect(screen.getByRole("heading", { name: "alpha/a.md" })).toBeTruthy();
    expect(vault.note).toHaveBeenCalledTimes(3);
    noNoteWrites(vault);
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});
test("a Vault target from another active workspace refuses without popping or reading", async () => {
  vi.useFakeTimers();
  try {
    const vault = noteNavigationApi();
    const { shell } = mount({ peers: true, vault });
    await settled();
    await click(
      within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
        "button",
        { name: "Vault" },
      ),
    );
    await vaultListing();
    await click(noteRow("A"));
    await click(screen.getByRole("button", { name: "App menu" }));
    await click(screen.getByRole("menuitem", { name: "Switch workspace" }));
    const reads = shell.calls.filter((call) => call.method === "workspaces").length;
    const original = shell.data.workspaces;
    const other = workspaceFixture({ workspace_id: "77777777-7777-4777-8777-777777777777" });
    expect(other.workspace_id).not.toBe(original.active_id);
    shell.data.workspaces = {
      ...original,
      workspaces: [...original.workspaces, other],
      active_id: other.workspace_id,
    };
    await click(screen.getByRole("button", { name: "Refresh" }));
    expect(shell.calls.filter((call) => call.method === "workspaces")).toHaveLength(reads + 1);
    await click(back());
    expect(screen.queryByRole("region", { name: "Vault browser" })).toBeNull();
    expect(vault.note).toHaveBeenCalledTimes(1);
    shell.data.workspaces = original;
    await click(screen.getByRole("button", { name: "Refresh" }));
    expect(shell.calls.filter((call) => call.method === "workspaces")).toHaveLength(reads + 2);
    await click(back());
    await vaultListing();
    expect(screen.getByRole("heading", { name: "alpha/a.md" })).toBeTruthy();
    expect(vi.mocked(vault.note).mock.calls.map(([id, ref]) => [id, ref])).toEqual([
      [original.active_id, "alpha/a.md"],
      [original.active_id, "alpha/a.md"],
    ]);
    noNoteWrites(vault);
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});

test("a selected App menu command leaves Vault and Back restores its current ref", async () => {
  vi.useFakeTimers();
  try {
    const vault = noteNavigationApi();
    await noteHistory(vault);
    await click(screen.getByRole("button", { name: "App menu" }));
    const menu = screen.getByRole("menu", { name: "App menu" });
    await click(within(menu).getByRole("menuitem", { name: /Settings/ }));
    expect(screen.queryByRole("region", { name: "Vault browser" })).toBeNull();
    expect(screen.queryByRole("menu")).toBeNull();
    await click(back());
    await vaultListing();
    expect(screen.getByRole("heading", { name: "alpha/b.md" })).toBeTruthy();
    expect(vault.note).toHaveBeenCalledTimes(3);
    await click(back());
    await vaultListing();
    expect(screen.getByRole("heading", { name: "alpha/a.md" })).toBeTruthy();
    expect(vault.note).toHaveBeenCalledTimes(4);
    noNoteWrites(vault);
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});

test.each(["preview", "apply"])(
  "canceled note-operation %s blocks actual App Back and Settings until completion",
  async (kind) => {
    vi.useFakeTimers();
    try {
      const vault = noteNavigationApi();
      const plan: Awaited<ReturnType<VaultOpsApi["preview"]>> = {
        steps: [],
        creates_folders: [],
        recovery_days: 30,
      };
      let finishPreview: ((value: typeof plan) => void) | undefined;
      let finishApply: ((value: Awaited<ReturnType<VaultOpsApi["apply"]>>) => void) | undefined;
      const ops: VaultOpsApi = {
        preview: vi.fn(
          kind === "preview"
            ? () =>
                new Promise<Awaited<ReturnType<VaultOpsApi["preview"]>>>((resolve) => {
                  finishPreview = resolve;
                })
            : async () => plan,
        ),
        apply: vi.fn(
          () =>
            new Promise<Awaited<ReturnType<VaultOpsApi["apply"]>>>((resolve) => {
              finishApply = resolve;
            }),
        ),
        recovery: vi.fn(async () => ({ entries: [], stuck: [] })),
        forgetOperation: vi.fn(),
        resolve: vi.fn(),
      };
      mount({ peers: true, vault, ops });
      await settled();
      await click(
        within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
          "button",
          { name: "Vault" },
        ),
      );
      await vaultListing();
      await click(noteRow("A"));
      await click(noteRow("B"));
      await click(screen.getByRole("button", { name: "Note actions" }));
      await click(screen.getByRole("menuitem", { name: "Delete" }));
      await click(screen.getByRole("button", { name: "Review" }));
      if (kind === "apply") await click(screen.getByRole("button", { name: "Apply" }));
      await click(screen.getByRole("button", { name: "Cancel" }));
      await click(screen.getByRole("button", { name: "Edit note" }));
      const editor = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Markdown" });
      fireEvent.change(editor, { target: { value: "held operation B" } });
      editor.focus();
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
      await click(back());
      await noteSettings();
      expect(screen.getByRole("textbox", { name: "Markdown" })).toBe(editor);
      expect(editor.value).toBe("held operation B");
      expect(document.activeElement).toBe(editor);
      expect(vault.note).toHaveBeenCalledTimes(2);
      expect(confirm).not.toHaveBeenCalled();
      await act(async () => {
        if (kind === "preview") {
          if (!finishPreview) throw new Error("Missing actual preview promise");
          finishPreview(plan);
        } else {
          if (!finishApply) throw new Error("Missing actual apply promise");
          finishApply({
            operation_id: workspaceFixture().workspace_id,
            status: "done",
            replayed: false,
            steps_applied: 1,
            reason: null,
            created_folders: [],
          });
        }
      });
      await click(back());
      await vaultListing();
      expect(confirm).toHaveBeenCalledExactlyOnceWith(
        "Your unsaved changes will be lost. Leave this view?",
      );
      expect(screen.getByRole("heading", { name: "alpha/a.md" })).toBeTruthy();
      expect(vault.note).toHaveBeenCalledTimes(3);
      expect(ops.preview).toHaveBeenCalledTimes(1);
      expect(ops.apply).toHaveBeenCalledTimes(kind === "preview" ? 0 : 1);
      noNoteWrites(vault);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  },
);
