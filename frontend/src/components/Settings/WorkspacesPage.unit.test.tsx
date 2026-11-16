/** WorkspacesPage's own page-level tests: composition of its parts, the read-failure states, the
 * tick write and its pruning after a removal or a policy change, removal itself, and
 * `onOpenInCode`. Each part's own mechanics (VaultCard, OtherVaults, AddFolders, WorkspaceList,
 * WorkspaceRowMenu) have their own test files; nothing here re-proves them. */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type {
  Capabilities,
  SettingsPatchResult,
  SettingsView,
  WorkspacesListing,
} from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import type { CommandArg, CommandId } from "../../commands";
import type { ShellSnapshot, ShellStore } from "../../state/shell";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  inspectionFixture,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import { WorkspacesPage } from "./WorkspacesPage";

afterEach(cleanup);

function snapshotFixture(
  capabilities: Capabilities,
  workspaces: WorkspacesListing | null,
  settings: SettingsView | null = settingsFixture(),
): ShellSnapshot {
  return {
    capabilities,
    capabilitiesStatus: "ready",
    settings,
    settingsStatus: settings === null ? "unavailable" : "ready",
    workspaces,
    workspacesStatus: workspaces === null ? "unavailable" : "ready",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
  };
}

function fakeStore(
  snapshot: ShellSnapshot = snapshotFixture(capabilitiesFixture(), listingFixture([])),
): ShellStore {
  return {
    getSnapshot: () => snapshot,
    subscribe: () => () => {},
    refreshCapabilities: vi.fn(async () => {}),
    refreshSettings: vi.fn(async () => {}),
    refreshWorkspaces: vi.fn(async () => {}),
    patch: vi.fn(async () => ({ ...settingsFixture(), command_id: "cmd", replayed: false })),
    adoptSettings: vi.fn(),
    runCheck: vi.fn(async () => {
      throw new Error("not used by WorkspacesPage");
    }),
    afterMutation: vi.fn(async () => {}),
    start: vi.fn(),
    stop: vi.fn(),
  };
}

const onCommand = vi.fn() as (id: CommandId, arg?: CommandArg) => void;

/** A settings document whose `chat.workspace_ids` is already the given ids, JSON-encoded exactly
 * as the page itself writes them. */
function tickedSettings(ids: readonly string[]): SettingsView {
  const base = settingsFixture();
  return { ...base, effective: { ...base.effective, "chat.workspace_ids": JSON.stringify(ids) } };
}

function tickableCode(overrides: Parameters<typeof workspaceFixture>[0] = {}) {
  return workspaceFixture({
    workspace_id: FAKE_WORKSPACE_ID,
    kind: "code",
    name: "Project",
    policy: "ordinary",
    persistent: true,
    root_available: true,
    ...overrides,
  });
}

test("renders its parts in order: title, lead, vault card, other vaults, add folders, workspace list", () => {
  const ACTIVE_ID = "11111111-1111-4111-8111-111111111111";
  const OTHER_ID = "11111111-1111-4111-8111-111111111112";
  const CODE_ID = "44444444-4444-4444-8444-444444444444";
  const active = workspaceFixture({ workspace_id: ACTIVE_ID, name: "Active vault" });
  const other = workspaceFixture({ workspace_id: OTHER_ID, name: "Other vault" });
  const code = workspaceFixture({
    workspace_id: CODE_ID,
    kind: "code",
    name: "Code project",
    policy: "existing_readonly",
  });
  const listing = listingFixture([active, other, code]);
  const capabilities = capabilitiesFixture({
    workspace: {
      chat_only: false,
      active: {
        id: ACTIVE_ID,
        kind: "vault",
        name: active.name,
        policy: active.policy,
        layout: "unknown",
        source: active.source,
        aliased: false,
        root_available: true,
      },
    },
  });
  const { container } = render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  const text = container.textContent ?? "";
  const order = [
    "Workspaces and Vault",
    "Your vault holds notes and is where chats start. Code folders below are additional places for chats to work.",
    "Active vault",
    "Other vault",
    "Code folders · extra folders for new chats",
    "Code project",
  ];
  let last = -1;
  for (const needle of order) {
    const at = text.indexOf(needle);
    expect(at, `"${needle}" should appear, after position ${last}`).toBeGreaterThan(last);
    last = at;
  }
});

test("a failed first read shows the status line in place of the content", () => {
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const snapshot: ShellSnapshot = {
    ...snapshotFixture(capabilities, null),
    workspacesStatus: "error",
    errors: { capabilities: "", settings: "", workspaces: "unavailable" },
  };
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshot}
      onCommand={onCommand}
    />,
  );
  expect(screen.getByRole("status").textContent).toContain("The operation could not be completed.");
  expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();
  expect(screen.queryByText("Code folders · extra folders for new chats")).toBeNull();
});

test("a failed refresh keeps the stale content and the status line beside it; Refresh re-reads both kinds", () => {
  const workspace = workspaceFixture();
  const listing = listingFixture([workspace]);
  const capabilities = capabilitiesFixture({
    workspace: {
      chat_only: false,
      active: {
        id: workspace.workspace_id,
        kind: "vault",
        name: workspace.name,
        policy: workspace.policy,
        layout: "unknown",
        source: workspace.source,
        aliased: false,
        root_available: true,
      },
    },
  });
  const store = fakeStore();
  const snapshot: ShellSnapshot = {
    ...snapshotFixture(capabilities, listing),
    workspacesStatus: "error",
    errors: { capabilities: "", settings: "", workspaces: "network_error" },
  };
  render(
    <WorkspacesPage api={fakeShellApi()} store={store} snapshot={snapshot} onCommand={onCommand} />,
  );
  expect(screen.getByRole("status").textContent).toContain("The server could not be reached.");
  expect(screen.getByText(workspace.name)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(store.afterMutation).toHaveBeenCalledWith(["capabilities", "workspaces"]);
});

test('a stale errors.workspaces code stays silent while a retry is only "loading"', () => {
  const listing = listingFixture([workspaceFixture()]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: false } });
  const snapshot: ShellSnapshot = {
    ...snapshotFixture(capabilities, listing),
    workspacesStatus: "loading",
    errors: { capabilities: "", settings: "", workspaces: "unavailable" },
  };
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshot}
      onCommand={onCommand}
    />,
  );
  expect(screen.queryByText("The operation could not be completed.")).toBeNull();
  // The quiet foot-of-page Refresh shows whenever there is data and the read has not
  // failed outright — true here even mid-retry, unlike the (absent) failure banner above.
  expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();
  expect(screen.getByText("My notes")).toBeTruthy();
});

test("a quiet Refresh re-reads once the page has data, even without a failure", () => {
  const listing = listingFixture([workspaceFixture()]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const store = fakeStore();
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={store}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(store.afterMutation).toHaveBeenCalledWith(["capabilities", "workspaces"]);
});

test("a tick patches chat.workspace_ids", async () => {
  const listing = listingFixture([tickableCode()]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const store = fakeStore();
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={store}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("checkbox"));
  await waitFor(() =>
    expect(store.patch).toHaveBeenCalledWith({
      "chat.workspace_ids": JSON.stringify([FAKE_WORKSPACE_ID]),
    }),
  );
});

test.each([
  ["settings have not loaded yet", null] as const,
  ["the settings document is read-only", { ...settingsFixture(), read_only: true }] as const,
])("ticking is blocked when %s", (_label, settings) => {
  const listing = listingFixture([tickableCode()]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const store = fakeStore();
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={store}
      snapshot={snapshotFixture(capabilities, listing, settings)}
      onCommand={onCommand}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  const checkbox = within(row).getByRole("checkbox");
  expect(checkbox.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(checkbox);
  expect(store.patch).not.toHaveBeenCalled();
});

test("a tick in flight blocks a second tick until it resolves", async () => {
  const CODE_B = "55555555-5555-4555-8555-555555555555";
  const rowA = tickableCode({ name: "Project A" });
  const rowB = tickableCode({ workspace_id: CODE_B, name: "Project B" });
  const listing = listingFixture([rowA, rowB]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const store = fakeStore();
  let resolvePatch: (() => void) | undefined;
  store.patch = vi.fn(
    () =>
      new Promise<SettingsPatchResult>((resolve) => {
        resolvePatch = () => resolve({ ...settingsFixture(), command_id: "cmd", replayed: false });
      }),
  );
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={store}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  const rowAEl = screen.getByText("Project A").closest("li") as HTMLElement;
  fireEvent.click(within(rowAEl).getByRole("checkbox"));
  await waitFor(() => expect(store.patch).toHaveBeenCalledTimes(1));
  const rowBEl = screen.getByText("Project B").closest("li") as HTMLElement;
  const checkboxB = within(rowBEl).getByRole("checkbox");
  expect(checkboxB.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(checkboxB);
  expect(store.patch).toHaveBeenCalledTimes(1);
  resolvePatch?.();
});

test("a corrupt chat.workspace_ids reads as no ticks, not a crash", () => {
  const listing = listingFixture([tickableCode()]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const base = settingsFixture();
  const settings = { ...base, effective: { ...base.effective, "chat.workspace_ids": "{not json" } };
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing, settings)}
      onCommand={onCommand}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  const checkbox = within(row).getByRole("checkbox") as HTMLInputElement;
  expect(checkbox.checked).toBe(false);
});

test("a failed tick write shows the refusal in settings-error", async () => {
  const listing = listingFixture([tickableCode()]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const store = fakeStore();
  store.patch = vi.fn(async () => {
    throw new ShellApiError(503, "busy");
  });
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={store}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("checkbox"));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toBeTruthy());
  expect(document.querySelector(".settings-error")).toBeTruthy();
});

test("removing a missing workspace prunes it from the ticks and refreshes", async () => {
  const code = tickableCode({ policy: "existing_readonly", root_available: false });
  const listing = listingFixture([code]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const settings = tickedSettings([FAKE_WORKSPACE_ID]);
  const api = fakeShellApi();
  const store = fakeStore();
  render(
    <WorkspacesPage
      api={api}
      store={store}
      snapshot={snapshotFixture(capabilities, listing, settings)}
      onCommand={onCommand}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Remove" }));
  const dialog = within(row).getByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
  await waitFor(() => expect(api.calls.some((c) => c.method === "removeWorkspace")).toBe(true));
  await waitFor(() =>
    expect(store.patch).toHaveBeenCalledWith({ "chat.workspace_ids": JSON.stringify([]) }),
  );
  expect(store.afterMutation).toHaveBeenCalledWith(["capabilities", "workspaces"]);
});

test("a 404 on remove is treated as already gone, not an error", async () => {
  const code = tickableCode({ policy: "existing_readonly", root_available: false });
  const listing = listingFixture([code]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const api = fakeShellApi();
  api.fail("removeWorkspace", new ShellApiError(404, "unknown_workspace"));
  const store = fakeStore();
  render(
    <WorkspacesPage
      api={api}
      store={store}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Remove" }));
  const dialog = within(row).getByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
  await waitFor(() =>
    expect(store.afterMutation).toHaveBeenCalledWith(["capabilities", "workspaces"]),
  );
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

test("a non-404 remove failure keeps the dialog open with the refusal", async () => {
  const code = tickableCode({ policy: "existing_readonly", root_available: false });
  const listing = listingFixture([code]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const api = fakeShellApi();
  api.fail("removeWorkspace", new ShellApiError(503, "busy"));
  render(
    <WorkspacesPage
      api={api}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Remove" }));
  const dialog = within(row).getByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
  await waitFor(() => expect(within(dialog).getByRole("alert")).toBeTruthy());
  expect(screen.getByRole("alertdialog")).toBeTruthy();
});

test("stopping trust through Details prunes a ticked id that is no longer ordinary", async () => {
  const code = tickableCode();
  const listing = listingFixture([code]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const settings = tickedSettings([FAKE_WORKSPACE_ID]);
  const snapshot = snapshotFixture(capabilities, listing, settings);
  const api = fakeShellApi({
    inspection: inspectionFixture({
      workspace_id: FAKE_WORKSPACE_ID,
      policy: "ordinary",
      acknowledgements_required: [],
    }),
  });
  const refreshedListing = listingFixture([{ ...code, policy: "existing_readonly" }]);
  const store = fakeStore({ ...snapshot, workspaces: refreshedListing });
  render(<WorkspacesPage api={api} store={store} snapshot={snapshot} onCommand={onCommand} />);
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "More actions for Project" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Details and trust" }));
  await waitFor(() =>
    expect(within(row).getByRole("button", { name: "Stop trusting" })).toBeTruthy(),
  );
  fireEvent.click(within(row).getByRole("button", { name: "Stop trusting" }));
  fireEvent.click(within(row).getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(store.refreshWorkspaces).toHaveBeenCalled());
  await waitFor(() =>
    expect(store.patch).toHaveBeenCalledWith({ "chat.workspace_ids": JSON.stringify([]) }),
  );
});

test("pressing an untrusted row's Trust… opens its panel and moves focus onto Trust this folder", async () => {
  const code = tickableCode({ policy: "existing_readonly" });
  const listing = listingFixture([code]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "Trust…" }));
  await waitFor(() =>
    expect(document.activeElement).toBe(
      within(row).getByRole("button", { name: "Trust this folder" }),
    ),
  );
  expect(within(row).queryByRole("button", { name: "Trust…" })).toBeNull();
});

test("every button this page renders carries a styled variant or is a settings-pill", () => {
  const ACTIVE_ID = "11111111-1111-4111-8111-111111111111";
  const OTHER_ID = "11111111-1111-4111-8111-111111111112";
  const UNTRUSTED_ID = "44444444-4444-4444-8444-444444444441";
  const MISSING_ID = "44444444-4444-4444-8444-444444444442";
  const active = workspaceFixture({ workspace_id: ACTIVE_ID, name: "Active vault" });
  const other = workspaceFixture({ workspace_id: OTHER_ID, name: "Other vault" });
  const untrusted = workspaceFixture({
    workspace_id: UNTRUSTED_ID,
    kind: "code",
    name: "Untrusted project",
    policy: "existing_readonly",
  });
  const missing = workspaceFixture({
    workspace_id: MISSING_ID,
    kind: "code",
    name: "Missing project",
    root_available: false,
  });
  const listing = listingFixture([active, other, untrusted, missing]);
  const capabilities = capabilitiesFixture({
    workspace: {
      chat_only: false,
      active: {
        id: ACTIVE_ID,
        kind: "vault",
        name: active.name,
        policy: active.policy,
        layout: "unknown",
        source: active.source,
        aliased: false,
        root_available: true,
      },
    },
  });
  const { container } = render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
    />,
  );
  const buttons = Array.from(container.querySelectorAll("button"));
  // Vault card (3) + Other vaults' two Use buttons + the add-folders form's submit + two code
  // rows' own ⋯ trigger and hint button each + the foot Refresh, at minimum.
  expect(buttons.length).toBeGreaterThan(8);
  for (const button of buttons) {
    const classes = button.className.split(/\s+/).filter(Boolean);
    const isPill = classes.includes("settings-pill");
    const isBtnVariant =
      classes.includes("btn") &&
      (classes.includes("go") || classes.includes("alt") || classes.includes("txt"));
    expect(isPill || isBtnVariant).toBe(true);
  }
});

test("Open in Code calls onOpenInCode with the row's id", async () => {
  const code = tickableCode();
  const listing = listingFixture([code]);
  const capabilities = capabilitiesFixture({ workspace: { chat_only: true } });
  const onOpenInCode = vi.fn();
  render(
    <WorkspacesPage
      api={fakeShellApi()}
      store={fakeStore()}
      snapshot={snapshotFixture(capabilities, listing)}
      onCommand={onCommand}
      onOpenInCode={onOpenInCode}
    />,
  );
  const row = screen.getByText("Project").closest("li") as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "More actions for Project" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Open in Code" }));
  expect(onOpenInCode).toHaveBeenCalledWith(FAKE_WORKSPACE_ID);
});
